// shell-capture-parser parses one shell script with the pinned reputable
// Bash grammar (mvdan.cc/sh/v3/syntax v3.11.0) and classifies it for the
// Obsidian capture pipeline. It is parse-only: it never evaluates, expands,
// or executes any shell input.
//
// stdin JSON:  {"schema": "shell-capture-parser@1", "command": "<script>"}
// stdout JSON:
//
//	{"status": "analyze",   "flags": [...] }
//	{"status": "deny",      "reason_code": "<fixed code>"}
//	{"status": "unsupported","reason_code": "<fixed code>"}
//
// Every unknown or unmodeled syntax element fails closed with a fixed
// reason code. Output never echoes the input text.
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"runtime/debug"
	"sort"
	"strings"
	"time"

	"mvdan.cc/sh/v3/syntax"
)

const (
	schemaVersion = "shell-capture-parser@1"
	maxInputBytes = 262144
	maxNodes      = 250000
	maxDepth      = 64
	watchdogCode  = 124
)

// budget anchors the whole-process wall-time watchdog. It is a variable
// so tests can exercise expiry through the terminateProcess seam without
// a production environment bypass.
var budget = 2 * time.Second

type result struct {
	Schema     string   `json:"schema"`
	Status     string   `json:"status"`
	ReasonCode string   `json:"reason_code,omitempty"`
	Flags      []string `json:"flags,omitempty"`
}

type request struct {
	Schema  string `json:"schema"`
	Command string `json:"command"`
}

var denyCodes = map[string]bool{
	"QUOTING_UNSUPPORTED":                   true,
	"PARSE_INVALID":                         true,
	"INVALID_SCHEMA":                        true,
	"INPUT_TOO_LARGE":                       true,
	"AST_LIMIT_EXCEEDED":                    true,
	"PARSE_TIMEOUT":                         true,
	"ARITHMETIC_UNSUPPORTED":                true,
	"BRACE_EXPANSION_UNSUPPORTED":           true,
	"ARRAY_ASSIGNMENT_UNSUPPORTED":          true,
	"REDIRECTION_OP_UNSUPPORTED":            true,
	"HEREDOC_DELIMITER_UNSUPPORTED":         true,
	"HEREDOC_LINE_CONTINUATION_UNSUPPORTED": true,
	"UNSUPPORTED_SYNTAX":                    true,
	"INPUT_HELPER_FAILURE":                  true,
}

var unsupportedCodes = map[string]bool{
	"INTERPRETER_EXECUTOR":             true,
	"INTERPRETER_HEREDOC_BODY":         true,
	"DYNAMIC_COMMAND_NAME":             true,
	"HIDDEN_EXECUTION_UNSUPPORTED":     true,
	"FUNCTION_DECLARATION_UNSUPPORTED": true,
	"WRAPPER_DISPATCH_UNSUPPORTED":     true,
	"GIT_DYNAMIC_INVOCATION":           true,
}

var interpreterBasenames = map[string]bool{
	"bash": true, "sh": true, "zsh": true, "dash": true, "ash": true,
	"ksh": true, "ksh93": true, "mksh": true,
}

var interpreterBuiltins = map[string]bool{
	"eval": true, "source": true, ".": true,
}

// hiddenExecutionBuiltins schedule, redefine, or remove hidden command
// behavior that this parser does not model; any of them fails closed.
var hiddenExecutionBuiltins = map[string]bool{
	"trap": true, "alias": true, "unalias": true, "shopt": true,
}

// wrapperDispatchers wrap or re-dispatch the effective command under
// option semantics this parser does not model, including env -S; all of
// them fail closed.
var wrapperDispatchers = map[string]bool{
	"command": true, "builtin": true, "env": true, "exec": true,
}

var heredocOperators = map[syntax.RedirOperator]bool{
	syntax.Hdoc:     true,
	syntax.DashHdoc: true,
}

var acceptedOperators = map[syntax.RedirOperator]bool{
	syntax.RdrOut: true, syntax.AppOut: true, syntax.RdrIn: true,
	syntax.RdrInOut: true, syntax.DplIn: true, syntax.DplOut: true,
	syntax.ClbOut: true, syntax.Hdoc: true, syntax.DashHdoc: true,
}

type walker struct {
	flags map[string]bool
	nodes int
	// startedAt anchors the parse and walk budget.
	startedAt time.Time
	// single records len(file.Stmts) == 1 for the simple-commit shape.
	single bool
	// allowSimple marks that the statement currently being classified
	// carries none of the extra statement features (background, negation,
	// redirects, compound nesting) that disqualify the simple shape.
	allowSimple bool
	// sawComment disqualifies the simple shape: the parser drops comments,
	// while the native word list keeps them as arguments.
	sawComment bool
	// sawHeredoc records any accepted here-document redirection, used by
	// the raw-source backslash-newline discrepancy guard.
	sawHeredoc bool
}

func (w *walker) flag(name string) {
	w.flags[name] = true
}

func (w *walker) check(depth int) string {
	w.nodes++
	if w.nodes > maxNodes || depth > maxDepth {
		return "AST_LIMIT_EXCEEDED"
	}
	if time.Since(w.startedAt) > budget {
		return "PARSE_TIMEOUT"
	}
	return ""
}

func (w *walker) stmts(list []*syntax.Stmt, depth, nested int) string {
	for _, stmt := range list {
		if failure := w.stmt(stmt, depth, nested); failure != "" {
			return failure
		}
	}
	return ""
}

func (w *walker) stmt(node *syntax.Stmt, depth, nested int) string {
	if failure := w.check(depth); failure != "" {
		return failure
	}
	// Background, negation, coprocess membership, or any route through a
	// redirect disqualifies this statement from the standalone simple shape.
	w.allowSimple = w.single && nested == 0 && !node.Background &&
		!node.Negated && !node.Coprocess && len(node.Redirs) == 0
	var interpreter bool
	if call, ok := node.Cmd.(*syntax.CallExpr); ok {
		argv, dynamic := w.callWords(call)
		interpreter = isInterpreterCommand(argv, dynamic)
	}
	for _, redirect := range node.Redirs {
		if failure := w.redirect(redirect, interpreter, depth+1, nested); failure != "" {
			return failure
		}
	}
	return w.command(node.Cmd, depth, nested)
}

func (w *walker) command(cmd syntax.Node, depth, nested int) string {
	switch typed := cmd.(type) {
	case nil:
		return ""
	case *syntax.CallExpr:
		return w.call(typed, depth, nested)
	case *syntax.BinaryCmd:
		if failure := w.stmt(typed.X, depth, nested+1); failure != "" {
			return failure
		}
		return w.stmt(typed.Y, depth, nested+1)
	case *syntax.Subshell:
		return w.stmts(typed.Stmts, depth+1, nested+1)
	case *syntax.Block:
		return w.stmts(typed.Stmts, depth+1, nested+1)
	case *syntax.IfClause:
		if failure := w.stmts(typed.Cond, depth+1, nested+1); failure != "" {
			return failure
		}
		if failure := w.stmts(typed.Then, depth+1, nested+1); failure != "" {
			return failure
		}
		if typed.Else == nil {
			return ""
		}
		return w.command(typed.Else, depth, nested+1)
	case *syntax.WhileClause:
		if failure := w.stmts(typed.Cond, depth+1, nested+1); failure != "" {
			return failure
		}
		return w.stmts(typed.Do, depth+1, nested+1)
	case *syntax.ForClause:
		if failure := w.loop(typed.Loop, depth+1); failure != "" {
			return failure
		}
		return w.stmts(typed.Do, depth+1, nested+1)
	case *syntax.CaseClause:
		if failure := w.word(typed.Word, depth, nested+1); failure != "" {
			return failure
		}
		for _, item := range typed.Items {
			for _, pattern := range item.Patterns {
				if failure := w.word(pattern, depth, nested+1); failure != "" {
					return failure
				}
			}
			if failure := w.stmts(item.Stmts, depth+1, nested+1); failure != "" {
				return failure
			}
		}
		return ""
	case *syntax.FuncDecl:
		return "FUNCTION_DECLARATION_UNSUPPORTED"
	case *syntax.TimeClause:
		if typed.Stmt == nil {
			return ""
		}
		return w.stmt(typed.Stmt, depth, nested+1)
	case *syntax.DeclClause:
		for _, assign := range typed.Args {
			if failure := w.assign(assign, depth); failure != "" {
				return failure
			}
		}
		return ""
	case *syntax.TestClause:
		return w.testExpr(typed.X, depth+1, nested+1)
	case *syntax.LetClause:
		return "ARITHMETIC_UNSUPPORTED"
	case *syntax.ArithmCmd:
		return "ARITHMETIC_UNSUPPORTED"
	case *syntax.CoprocClause:
		if typed.Stmt == nil {
			return ""
		}
		return w.stmt(typed.Stmt, depth+1, nested+1)
	default:
		return "UNSUPPORTED_SYNTAX"
	}
}

func (w *walker) loop(loop syntax.Loop, depth int) string {
	switch typed := loop.(type) {
	case *syntax.WordIter:
		for _, item := range typed.Items {
			if failure := w.word(item, depth, 1); failure != "" {
				return failure
			}
		}
		return ""
	case *syntax.CStyleLoop:
		return "ARITHMETIC_UNSUPPORTED"
	default:
		return "UNSUPPORTED_SYNTAX"
	}
}

func (w *walker) testExpr(expr syntax.TestExpr, depth, nested int) string {
	switch typed := expr.(type) {
	case nil:
		return ""
	case *syntax.Word:
		return w.word(typed, depth, nested)
	case *syntax.BinaryTest:
		if failure := w.testExpr(typed.X, depth+1, nested); failure != "" {
			return failure
		}
		return w.testExpr(typed.Y, depth+1, nested)
	case *syntax.UnaryTest:
		return w.testExpr(typed.X, depth+1, nested)
	case *syntax.ParenTest:
		return w.testExpr(typed.X, depth+1, nested)
	default:
		return "UNSUPPORTED_SYNTAX"
	}
}

func (w *walker) assign(node *syntax.Assign, depth int) string {
	if node.Index != nil || node.Array != nil {
		return "ARRAY_ASSIGNMENT_UNSUPPORTED"
	}
	if node.Value == nil {
		return ""
	}
	return w.word(node.Value, depth, 1)
}

func (w *walker) redirect(node *syntax.Redirect, interpreter bool, depth, nested int) string {
	if !acceptedOperators[node.Op] {
		return "REDIRECTION_OP_UNSUPPORTED"
	}
	if heredocOperators[node.Op] {
		// The pinned parser does not model Bash's line-continuation
		// behavior around here-documents the way the shell does; conservatively
		// reject any heredoc alongside a raw backslash-newline in the source.
		w.sawHeredoc = true
	}
	if !heredocOperators[node.Op] {
		if failure := w.word(node.Word, depth, nested+1); failure != "" {
			return failure
		}
		return ""
	}
	literalOnly := true
	for _, part := range node.Word.Parts {
		switch node := part.(type) {
		case *syntax.Lit:
		case *syntax.SglQuoted:
		case *syntax.DblQuoted:
			for _, inner := range node.Parts {
				if _, ok := inner.(*syntax.Lit); !ok {
					literalOnly = false
				}
			}
		default:
			literalOnly = false
		}
	}
	if !literalOnly {
		return "HEREDOC_DELIMITER_UNSUPPORTED"
	}
	if node.Hdoc == nil {
		return "HEREDOC_DELIMITER_UNSUPPORTED"
	}
	if interpreter {
		return "INTERPRETER_HEREDOC_BODY"
	}
	literalBody := true
	var dynamic []syntax.WordPart
	for _, part := range node.Hdoc.Parts {
		if _, ok := part.(*syntax.Lit); !ok {
			literalBody = false
			dynamic = append(dynamic, part)
		}
	}
	if !literalBody {
		w.flag("HEREDOC_EXPANSION")
	}
	for _, part := range dynamic {
		if failure := w.part(part, depth, nested+1); failure != "" {
			return failure
		}
	}
	return ""
}

func (w *walker) word(word *syntax.Word, depth, nested int) string {
	if word == nil {
		return "UNSUPPORTED_SYNTAX"
	}
	if hasBraceExpansion(word) {
		return "BRACE_EXPANSION_UNSUPPORTED"
	}
	for _, part := range word.Parts {
		if failure := w.part(part, depth, nested); failure != "" {
			return failure
		}
	}
	return ""
}

// wordShape joins a word's raw unquoted literals. Each other part stands in
// as one ordinary byte because it can sit inside an expansion or a pattern.
func wordShape(word *syntax.Word) string {
	var text strings.Builder
	for _, part := range word.Parts {
		if literal, ok := part.(*syntax.Lit); ok {
			text.WriteString(literal.Value)
		} else {
			text.WriteByte('x')
		}
	}
	return text.String()
}

// hasBracketGlob reports an unescaped [ with a later unescaped ] anywhere
// in a word. An unclosed bracket is literal, including the [ builtin.
func hasBracketGlob(word *syntax.Word) bool {
	raw := wordShape(word)
	opened := false
	for index := 0; index < len(raw); index++ {
		switch raw[index] {
		case '\\':
			index++
		case '[':
			opened = true
		case ']':
			if opened {
				return true
			}
		}
	}
	return false
}

// hasBraceExpansion reports whether a word would brace-expand in Bash.
// Quoted, escaped, and unexpanding braces such as {}, @{1}, and {owner}
// stay literal.
func hasBraceExpansion(word *syntax.Word) bool {
	raw := wordShape(word)
	var separators []bool
	for index := 0; index < len(raw); index++ {
		switch {
		case raw[index] == '\\':
			index++
		case raw[index] == '{':
			separators = append(separators, false)
		case len(separators) == 0:
		case raw[index] == ',',
			strings.HasPrefix(raw[index:], ".."):
			separators[len(separators)-1] = true
		case raw[index] == '}':
			last := len(separators) - 1
			if separators[last] {
				return true
			}
			separators = separators[:last]
		}
	}
	return false
}

// part sweeps one word part. It returns a failure code on unsupported
// syntax and never inspects plain literal characters for classification.
func (w *walker) part(node syntax.WordPart, depth, nested int) string {
	switch typed := node.(type) {
	case *syntax.Lit:
		return ""
	case *syntax.SglQuoted:
		return ""
	case *syntax.DblQuoted:
		return w.parts(typed.Parts, depth, nested)
	case *syntax.ParamExp:
		if typed.Index != nil {
			return "ARITHMETIC_UNSUPPORTED"
		}
		if typed.Slice != nil {
			return "ARITHMETIC_UNSUPPORTED"
		}
		if typed.Repl != nil {
			if failure := w.word(typed.Repl.Orig, depth, nested); failure != "" {
				return failure
			}
			if failure := w.word(typed.Repl.With, depth, nested); failure != "" {
				return failure
			}
		}
		if typed.Exp != nil && typed.Exp.Word != nil {
			return w.word(typed.Exp.Word, depth, nested)
		}
		return ""
	case *syntax.CmdSubst:
		return w.stmts(typed.Stmts, depth+1, nested+1)
	case *syntax.ProcSubst:
		return w.stmts(typed.Stmts, depth+1, nested+1)
	case *syntax.ArithmExp:
		return "ARITHMETIC_UNSUPPORTED"
	default:
		return "UNSUPPORTED_SYNTAX"
	}
}

func (w *walker) parts(list []syntax.WordPart, depth, nested int) string {
	for _, part := range list {
		if failure := w.part(part, depth, nested); failure != "" {
			return failure
		}
	}
	return ""
}

// unquoteLit removes Bash backslash escapes in an unquoted literal token:
// the backslash removes itself before the next character, and
// backslash-newline is a line continuation removed entirely.
func unquoteLit(value string) string {
	if !strings.Contains(value, "\\") {
		return value
	}
	var out strings.Builder
	for index := 0; index < len(value); index++ {
		if value[index] == '\\' && index+1 < len(value) {
			if value[index+1] == '\n' {
				index++
				continue
			}
			index++
			out.WriteByte(value[index])
			continue
		}
		out.WriteByte(value[index])
	}
	return out.String()
}

// unquoteDblQuoted applies double-quote backslash rules: the backslash is
// removed only before $, backtick, double quote, backslash, or newline;
// everywhere else it stays literal.
func unquoteDblQuoted(value string) string {
	if !strings.Contains(value, "\\") {
		return value
	}
	var out strings.Builder
	for index := 0; index < len(value); index++ {
		if value[index] == '\\' && index+1 < len(value) {
			switch value[index+1] {
			case '$', '`', '"', '\\', '\n':
				index++
				if value[index] == '\n' {
					continue
				}
			}
		}
		out.WriteByte(value[index])
	}
	return out.String()
}

// bareLitExpansive reports whether an unquoted literal part carries
// expansion candidates before quote removal: an unescaped * or ? is a
// pathname expansion candidate, and a tilde at word start or after = or :
// is a home-expansion candidate. Escaped or quoted characters stay literal;
// no filesystem or home expansion value is computed.
func bareLitExpansive(raw string, leading bool) bool {
	if leading && strings.HasPrefix(raw, "~") {
		return true
	}
	for index := 0; index < len(raw); index++ {
		if raw[index] == '\\' {
			// An escaped character is literal shell text, not a
			// pattern metacharacter.
			index++
			continue
		}
		if raw[index] == '*' || raw[index] == '?' {
			return true
		}
		if raw[index] == '~' && index > 0 &&
			(raw[index-1] == '=' || raw[index-1] == ':') {
			return true
		}
	}
	return false
}

// expandWord returns the quote-removed text of a word and whether every
// part is literal executable text. The third return value is a failure
// code for unsupported spelling, such as dollar quoting; callers must
// surface it instead of silently marking the word dynamic.
func (w *walker) expandWord(word *syntax.Word) (string, bool, string) {
	if word == nil {
		return "", false, "UNSUPPORTED_SYNTAX"
	}
	var builder strings.Builder
	static := !hasBracketGlob(word)
	for partIndex, part := range word.Parts {
		switch node := part.(type) {
		case *syntax.Lit:
			if bareLitExpansive(node.Value, partIndex == 0) {
				static = false
			}
			builder.WriteString(unquoteLit(node.Value))
		case *syntax.SglQuoted:
			if node.Dollar {
				return "", false, "QUOTING_UNSUPPORTED"
			}
			builder.WriteString(node.Value)
		case *syntax.DblQuoted:
			if node.Dollar {
				return "", false, "QUOTING_UNSUPPORTED"
			}
			for _, inner := range node.Parts {
				literal, ok := inner.(*syntax.Lit)
				if !ok {
					static = false
					break
				}
				builder.WriteString(unquoteDblQuoted(literal.Value))
			}
		default:
			static = false
		}
	}
	return builder.String(), static, ""
}

func (w *walker) callWords(call *syntax.CallExpr) ([]string, bool) {
	argv := []string{}
	dynamic := false
	for _, word := range call.Args {
		value, static, code := w.expandWord(word)
		if code != "" {
			return nil, false
		}
		argv = append(argv, value)
		if !static {
			dynamic = true
		}
	}
	return argv, dynamic
}

type wordRecord struct {
	value  string
	static bool
}

// wordValues flattens classified word records for word-list-only checks
// such as commit-word detection and the simple shape; the result is never
// produced as executor argv.
func wordValues(words []wordRecord) []string {
	values := make([]string, 0, len(words))
	for _, record := range words {
		values = append(values, record.value)
	}
	return values
}

// call classifies one command expression. nested > 0 marks that the call
// sits inside executable substitution or compound structure, so it can never
// qualify as the single simple commit candidate.
func (w *walker) call(call *syntax.CallExpr, depth, nested int) string {
	if failure := w.check(depth); failure != "" {
		return failure
	}
	for _, assign := range call.Assigns {
		if failure := w.assign(assign, depth); failure != "" {
			return failure
		}
	}
	var words []wordRecord
	dynamic := false
	for _, word := range call.Args {
		value, static, code := w.expandWord(word)
		if code != "" {
			return code
		}
		words = append(words, wordRecord{value: value, static: static})
		if !static {
			dynamic = true
		}
		if hasBraceExpansion(word) {
			return "BRACE_EXPANSION_UNSUPPORTED"
		}
		for _, part := range word.Parts {
			if failure := w.part(part, depth, nested); failure != "" {
				return failure
			}
		}
	}
	if dynamic {
		w.flag("DYNAMIC_WORDS")
	}
	if len(words) == 0 {
		return ""
	}
	// Normalize the supported rtk prefix once for classification only:
	// an rtk basename strips itself, then an optional literal proxy that
	// immediately follows it. A standalone external proxy command never
	// strips. The classified remainder is never produced as executor argv;
	// native keeps its own independent wrapper, root, and raw checks.
	rtkStripped := basename(words[0].value) == "rtk" && words[0].static
	if rtkStripped {
		rest := words[1:]
		if len(rest) > 0 && rest[0].value == "proxy" && rest[0].static {
			rest = rest[1:]
		}
		words = rest
		dynamic = false
		for _, record := range words {
			if !record.static {
				dynamic = true
			}
		}
		if len(words) == 0 {
			// A bare rtk invocation dispatches nothing classifiable here.
			return ""
		}
		// A second rtk basename after the one supported prefix is nested
		// dispatch this parser does not model; fail closed.
		if basename(words[0].value) == "rtk" {
			return "WRAPPER_DISPATCH_UNSUPPORTED"
		}
	}
	first := words[0]
	if !first.static {
		if rtkStripped {
			// Unknown or dynamic rtk dispatch targets are unsupported,
			// never ordinary and never executable argv.
			return "WRAPPER_DISPATCH_UNSUPPORTED"
		}
		return "DYNAMIC_COMMAND_NAME"
	}
	firstName := basename(first.value)
	if hiddenExecutionBuiltins[firstName] {
		return "HIDDEN_EXECUTION_UNSUPPORTED"
	}
	if wrapperDispatchers[firstName] {
		return "WRAPPER_DISPATCH_UNSUPPORTED"
	}
	if interpreterBuiltins[firstName] || interpreterBasenames[firstName] {
		return "INTERPRETER_EXECUTOR"
	}
	if containsCommitWords(wordValues(words)) {
		banned := false
		for _, record := range words {
			if record.value == "-c" || record.value == "--config-env" {
				banned = true
			}
		}
		simple := !banned && nested == 0 && w.allowSimple && !dynamic &&
			!w.sawComment && len(call.Assigns) == 0 &&
			simpleGitShape(wordValues(words))
		if simple {
			w.flag("GIT_COMMIT_SIMPLE")
		} else {
			w.flag("COMMIT_WORDS_DETECTED")
		}
		return ""
	}
	if firstName == "git" && gitSubcommandDynamic(words) {
		return "GIT_DYNAMIC_INVOCATION"
	}
	return ""
}

// gitSubcommandDynamic reports a dynamic word at the Git subcommand
// position, past static global options and their -C and -c values.
func gitSubcommandDynamic(words []wordRecord) bool {
	for index := 1; index < len(words); index++ {
		record := words[index]
		if !record.static {
			return true
		}
		if !strings.HasPrefix(record.value, "-") {
			return false
		}
		if record.value == "-C" || record.value == "-c" {
			index++
		}
	}
	return false
}

// simpleGitShape mirrors the native shell_commit word prefix: an optional
// rtk basename, an optional literal proxy only when it immediately follows
// that rtk basename, git or /usr/bin/git, one optional -C <root> pair, then
// commit in the actual subcommand position. It classifies the word list
// only; native performs its own independent stricter validation of the raw
// command, Git root, and argv, and the helper flag alone never authorizes
// execution.
func simpleGitShape(words []string) bool {
	index := 0
	if index < len(words) && basename(words[index]) == "rtk" {
		index++
		if index < len(words) && words[index] == "proxy" {
			index++
		}
	}
	if index >= len(words) || (words[index] != "git" && words[index] != "/usr/bin/git") {
		return false
	}
	index++
	if index+1 < len(words) && words[index] == "-C" {
		index += 2
	}
	return index < len(words) && words[index] == "commit"
}

func containsCommitWords(words []string) bool {
	commit := false
	git := false
	for _, word := range words {
		if word == "commit" {
			commit = true
		}
		// Only a git basename is a Git invocation; argument substrings
		// like "digit" or "digit-grep" must not infer Git.
		if basename(word) == "git" {
			git = true
		}
	}
	return commit && git
}

func isInterpreterCommand(argv []string, dynamic bool) bool {
	if dynamic || len(argv) == 0 {
		return false
	}
	first := basename(argv[0])
	return interpreterBuiltins[first] || interpreterBasenames[first]
}

func basename(word string) string {
	if index := strings.LastIndexAny(word, "/"); index >= 0 {
		return word[index+1:]
	}
	return word
}

// nodeBounds enforces AST size limits before classification with the
// pinned syntax.Walk traversal: every node kind (statements, calls, words,
// parameters, conditionals, redirections, comments) is counted against
// maxNodes, and the nil-leave callback pops the depth counter so tree
// nesting depth is bounded against maxDepth. It returns a fixed failure
// code, empty when within limits. Walk cannot abort midway, but the
// bounded input keeps the unpruned remainder finite.
func (w *walker) nodeBounds(file *syntax.File) string {
	violation := ""
	depth := 0
	visitor := func(node syntax.Node) bool {
		if node == nil {
			// Walk emits one nil-leave after each visited node's
			// children; every successful enter pops here.
			depth--
			return false
		}
		if _, comment := node.(*syntax.Comment); comment {
			w.sawComment = true
		}
		depth++
		w.nodes++
		if w.nodes > maxNodes || depth > maxDepth {
			if violation == "" {
				violation = "AST_LIMIT_EXCEEDED"
			}
			// Walk sends no nil-leave for this node, so the counter
			// stays high; the latched violation rejects the run.
			return false
		}
		return true
	}
	// Walk panics on unmodeled node types rather than returning a
	// failure; the caller's guard recovers fail closed.
	syntax.Walk(file, visitor)
	return violation
}

// terminateProcess is the watchdog's process-exit seam. Tests override
// this variable directly; there is no production environment bypass.
var terminateProcess = func(code int) {
	os.Exit(code)
}

// armWatchdog starts the whole-process wall-time budget covering stdin
// read, parse, and traversal. Expiry terminates the process with the
// fixed nonzero watchdog outcome and emits nothing.
func armWatchdog(startedAt time.Time) {
	remaining := budget - time.Since(startedAt)
	if remaining <= 0 {
		terminateProcess(watchdogCode)
		return
	}
	time.AfterFunc(remaining, func() {
		terminateProcess(watchdogCode)
	})
}

func analyze(command string) (result, string) {
	// outcome builds every analyze result so the response schema/version
	// is always present for strict downstream validation.
	outcome := func(status, reasonCode string, flags ...string) result {
		return result{Schema: schemaVersion, Status: status, ReasonCode: reasonCode, Flags: flags}
	}
	// The caller-side byte bound holds even when analyze is invoked
	// directly, so parsing never sees an oversized document.
	if len(command) > maxInputBytes {
		return outcome("deny", "INPUT_TOO_LARGE"), ""
	}
	w := &walker{flags: map[string]bool{}, startedAt: time.Now()}
	parser := syntax.NewParser(syntax.Variant(syntax.LangBash), syntax.KeepComments(true))
	file, err := parser.Parse(strings.NewReader(command), "command")
	// Parse wall clock: parsing happens first and unpreemptably, so the
	// whole-process budget includes it and is also rechecked here.
	if time.Since(w.startedAt) > budget {
		return outcome("deny", "PARSE_TIMEOUT"), ""
	}
	if err != nil {
		return outcome("deny", "PARSE_INVALID"), ""
	}
	w.single = len(file.Stmts) == 1
	// Bound the AST before classification. syntax.Walk recovers placed
	// here fail closed on any traversal fault from unmodeled nodes.
	bounded := func() (violation string) {
		defer func() {
			if recovered := recover(); recovered != nil {
				violation = "INPUT_HELPER_FAILURE"
			}
		}()
		return w.nodeBounds(file)
	}()
	if bounded != "" {
		return outcome("deny", bounded), ""
	}
	if failure := w.stmts(file.Stmts, 0, 0); failure != "" {
		if denyCodes[failure] {
			return outcome("deny", failure), ""
		}
		if unsupportedCodes[failure] {
			return outcome("unsupported", failure), ""
		}
		return outcome("deny", "INPUT_HELPER_FAILURE"), ""
	}
	// Conservative discrepancy guard: the pinned parser does not model
	// how Bash treats a raw backslash-newline around here-documents, so any
	// parsed heredoc combined with one in the source fails closed. This may
	// overdeny but never masks or reinterprets body text.
	if w.sawHeredoc && strings.Contains(command, "\\\n") {
		return outcome("deny", "HEREDOC_LINE_CONTINUATION_UNSUPPORTED"), ""
	}
	flags := make([]string, 0, len(w.flags))
	for name := range w.flags {
		flags = append(flags, name)
	}
	sort.Strings(flags)
	return outcome("analyze", "", flags...), ""
}

func emit(value result) {
	value.Schema = schemaVersion
	encoded, _ := json.Marshal(value)
	os.Stdout.Write(encoded)
	os.Stdout.Write([]byte("\n"))
}

func main() {
	// Fail closed: convert any panic (parser fault,
	// runtime fault) into the fixed helper-failure code, one line only.
	defer func() {
		if recovered := recover(); recovered != nil {
			emit(result{Status: "deny", ReasonCode: "INPUT_HELPER_FAILURE"})
			return
		}
	}()
	// Process-local soft limits, no OS or global settings change: the
	// memory ceiling is Go's soft garbage-collector limit, not a hard
	// guarantee against every allocation path; the stack cap hard-caps each
	// goroutine so a runaway pinned-parser recursion aborts. Exceeding the
	// stack faults the child before any output, which the caller treats as
	// a helper failure.
	debug.SetMemoryLimit(256 << 20)
	debug.SetMaxStack(64 << 20)
	armWatchdog(time.Now())
	raw, readErr := io.ReadAll(io.LimitReader(os.Stdin, maxInputBytes+1))
	if readErr != nil {
		emit(result{Status: "deny", ReasonCode: "INPUT_HELPER_FAILURE"})
		return
	}
	if len(raw) > maxInputBytes {
		emit(result{Status: "deny", ReasonCode: "INPUT_TOO_LARGE"})
		return
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var request request
	if decodeErr := decoder.Decode(&request); decodeErr != nil {
		emit(result{Status: "deny", ReasonCode: "INVALID_SCHEMA"})
		return
	}
	// Exactly one JSON request: any trailing JSON value or garbage after
	// the first request is malformed protocol, not a second request.
	if _, tailErr := decoder.Token(); tailErr != io.EOF {
		emit(result{Status: "deny", ReasonCode: "INVALID_SCHEMA"})
		return
	}
	if request.Schema != schemaVersion || request.Command == "" {
		emit(result{Status: "deny", ReasonCode: "INVALID_SCHEMA"})
		return
	}
	if len(request.Command) > maxInputBytes {
		emit(result{Status: "deny", ReasonCode: "INPUT_TOO_LARGE"})
		return
	}
	outcome, _ := analyze(request.Command)
	if outcome.Status == "" {
		emit(result{Status: "deny", ReasonCode: "INPUT_HELPER_FAILURE"})
		return
	}
	emit(outcome)
}
