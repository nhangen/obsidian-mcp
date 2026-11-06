package main

import (
	"encoding/json"
	"io"
	"strings"
	"testing"
	"time"
)

// blockerCases reproduce the five synthetic review blockers for the
// rejected python scanner, plus the two delimiter edge cases where the
// pinned parser fails closed on inputs bash accepts.
var blockerCases = []struct {
	name   string
	input  string
	status string
	deny   string
	flags  []string
}{
	{
		name:   "semicolon-after-delimiter",
		input:  "cat <<'END'\nordinary data\ngit commit -m fixture\nEND; echo tail",
		status: "deny",
		deny:   "PARSE_INVALID",
	},
	{
		name:   "terminator-then-next-command",
		input:  "cat <<'END'\nordinary data\ngit commit -m fixture\nEND\necho tail",
		status: "analyze",
		flags:  []string{},
	},
	{
		name:   "backslash-double-quoted-delimiter",
		input:  "cat <<\"E\\q\"\nbody\nE\\q",
		status: "analyze",
		flags:  []string{},
	},
	{
		name:   "backslash-continuation-body",
		input:  "cat <<END\nhi\nE\\\nND",
		status: "deny",
		deny:   "PARSE_INVALID",
	},
	{
		name:   "quoted-backslash-delimiter-unclosed",
		input:  "cat <<E\\q\nbody\nE\\q",
		status: "deny",
		deny:   "PARSE_INVALID",
	},
	{
		name:   "heredoc-line-continuation-full-fixture",
		input:  "cat <<END\nordinary data\nE\\\nND\ngit commit -m fixture\nEND\n",
		status: "deny",
		deny:   "HEREDOC_LINE_CONTINUATION_UNSUPPORTED",
	},
	{
		name:   "quoted-heredoc-ordinary-data",
		input:  "cat <<'PY'\nprint(\"it's data\")\nPY",
		status: "analyze",
		flags:  []string{},
	},
	{
		name:   "quoted-heredoc-backslash-newline-overdeny",
		input:  "cat <<'END'\nline one\\\nline two\nEND",
		status: "deny",
		deny:   "HEREDOC_LINE_CONTINUATION_UNSUPPORTED",
	},
	{
		name:   "comment-close-substitution",
		input:  "printf '%s' \"$(printf harmless # )\ngit commit -m fixture\n)\"",
		status: "analyze",
		flags:  []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"},
	},
	{
		name:   "interpreter-heredoc-body",
		input:  "bash <<'END'\n# it's data\ngit commit -m fixture\nEND",
		status: "unsupported",
		deny:   "INTERPRETER_HEREDOC_BODY",
	},
	{
		name:   "brace-expansion-argv",
		input:  "git commit -m {alpha,beta}",
		status: "deny",
		deny:   "BRACE_EXPANSION_UNSUPPORTED",
	},
}

func assertResult(t *testing.T, got result, wantStatus, wantDeny string, wantFlags []string) {
	t.Helper()
	if got.Status != wantStatus {
		t.Fatalf("status = %q, want %q", got.Status, wantStatus)
	}
	if got.ReasonCode != wantDeny {
		t.Fatalf("reason_code = %q, want %q", got.ReasonCode, wantDeny)
	}
	if wantStatus != "analyze" {
		return
	}
	if len(got.Flags) != len(wantFlags) {
		t.Fatalf("flags = %v, want %v", got.Flags, wantFlags)
	}
	for i, flag := range wantFlags {
		if got.Flags[i] != flag {
			t.Fatalf("flags = %v, want %v", got.Flags, wantFlags)
		}
	}
}

func TestAnalyzeBlockers(t *testing.T) {
	for _, tc := range blockerCases {
		t.Run(tc.name, func(t *testing.T) {
			got, _ := analyze(tc.input)
			assertResult(t, got, tc.status, tc.deny, tc.flags)
		})
	}
}

func TestAnalyzeClassification(t *testing.T) {
	cases := []struct {
		name   string
		input  string
		status string
		deny   string
		flags  []string
	}{
		{name: "simple-commit", input: "git commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "plain-command", input: "echo hello", status: "analyze", flags: []string{}},
		{name: "compound-arm", input: "true && git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "two-statements", input: "true; git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "dynamic-message", input: "git commit -m \"$msg\"", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "heredoc-message", input: "git commit -m \"$(cat <<'EOF'\nit's fine\nEOF\n)\"", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "stdin-heredoc", input: "git commit -F - <<'EOF'\nit's fine\nEOF\n", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "trailing-comment", input: "git commit -m fixture # --amend", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "comment-apostrophe", input: "git commit -m fixture # it's", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "hash-in-quotes", input: "git commit -m \"fix #12\"", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "bracket-glob-split-across-quotes", input: "git commit -m a[\"b\"]", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "bracket-glob-single-quoted-class", input: "git commit -m a['b']", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "unclosed-bracket-static", input: "git commit -m a[b", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "escaped-close-bracket-static", input: "git commit -m a[b\\]", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "tilde-after-equals", input: "git commit -m a=~/x", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "tilde-after-colon", input: "git commit -m x=1:~", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "escaped-tilde-after-equals", input: "git commit -m a=\\~/x", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "dynamic-subcommand-after-option", input: "git --no-pager \"$verb\" -m fixture", status: "unsupported", deny: "GIT_DYNAMIC_INVOCATION"},
		{name: "dynamic-subcommand-after-config", input: "git -c a=b \"$verb\" -m fixture", status: "unsupported", deny: "GIT_DYNAMIC_INVOCATION"},
		{name: "dynamic-subcommand-after-two-roots", input: "git -C a -C b \"$verb\" -m fixture", status: "unsupported", deny: "GIT_DYNAMIC_INVOCATION"},
		{name: "dynamic-root-static-subcommand", input: "git -C \"$root\" status", status: "analyze", flags: []string{"DYNAMIC_WORDS"}},
		{name: "test-builtin", input: "[ -f fixture ] && echo yes", status: "analyze", flags: []string{}},
		{name: "reflog-selector", input: "git show HEAD@{1}", status: "analyze", flags: []string{}},
		{name: "find-placeholder", input: "find . -exec ls {} \\;", status: "analyze", flags: []string{}},
		{name: "gh-placeholder", input: "gh api repos/{owner}/{repo}/pulls", status: "analyze", flags: []string{}},
		{name: "escaped-braces", input: "echo \\{a,b\\}", status: "analyze", flags: []string{}},
		{name: "brace-list", input: "echo {a,b}", status: "deny", deny: "BRACE_EXPANSION_UNSUPPORTED"},
		{name: "brace-range", input: "echo {1..3}", status: "deny", deny: "BRACE_EXPANSION_UNSUPPORTED"},
		{name: "brace-around-quote", input: "echo {a,\"b\"}", status: "deny", deny: "BRACE_EXPANSION_UNSUPPORTED"},
		{name: "dynamic-git-read", input: "git diff \"$BASE\"", status: "analyze", flags: []string{"DYNAMIC_WORDS"}},
		{name: "glob-git-add", input: "git add *.py", status: "analyze", flags: []string{"DYNAMIC_WORDS"}},
		{name: "substitution-body", input: "echo \"$(git commit -m fixture)\"", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "funcdecl-body", input: "f() { git commit -m fixture; }", status: "unsupported", deny: "FUNCTION_DECLARATION_UNSUPPORTED"},
		{name: "path-git", input: "/usr/bin/git commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "banned-config", input: "git -c x=y commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "dynamic-command", input: "\"$tool\" commit it", status: "unsupported", deny: "DYNAMIC_COMMAND_NAME"},
		{name: "interpreter-builtin", input: "eval echo hi", status: "unsupported", deny: "INTERPRETER_EXECUTOR"},
		{name: "interpreter-path", input: "/bin/bash -c true", status: "unsupported", deny: "INTERPRETER_EXECUTOR"},
		{name: "array-write", input: "arr=(x)", status: "deny", deny: "ARRAY_ASSIGNMENT_UNSUPPORTED"},
		{name: "arithmetic-flag", input: "git commit -m $((1+2))", status: "deny", deny: "ARITHMETIC_UNSUPPORTED"},
		{name: "test-clause", input: "[[ -f fix ]]", status: "analyze", flags: []string{}},
		{name: "until-loop", input: "until false; do true; done", status: "analyze", flags: []string{}},
		{name: "coproc", input: "coproc { echo hi; }", status: "analyze", flags: []string{}},
		{name: "dash-heredoc", input: "cat <<-END\n\tdata\n\tEND\necho z", status: "analyze", flags: []string{}},
		{name: "heredoc-expansion", input: "cat <<EOF\n$(git commit -m fixture)\nEOF", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "HEREDOC_EXPANSION"}},
		{name: "quoted-brace-literal", input: "git commit -m \"alpha{,beta}\"", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "escaped-name", input: "g\\it commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "escaped-name-and-subcommand", input: "g\\it c\\ommit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "escaped-contiguous-name", input: "g\\i\\t commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "quoted-concatenated-name", input: "g\"it\" commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "fully-quoted-name", input: "\"git\" commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "empty-quotes-concatenated", input: "gi\"\"t commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "empty-quotes-argument", input: "echo \"\"\"still static\"", status: "analyze", flags: []string{}},
		{name: "dq-backslash-preserved", input: "echo \"a\\b\"", status: "analyze", flags: []string{}},
		{name: "dq-backslash-before-g", input: "echo \"g\\it\"", status: "analyze", flags: []string{}},
		{name: "dq-backslash-sq-approx", input: "git commit -m \"a\\$b\"", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "dollar-single-quoted", input: "$'g\\x69t' commit -m fixture", status: "deny", deny: "QUOTING_UNSUPPORTED"},
		{name: "dollar-double-quoted", input: "$\"git\" commit -m fixture", status: "deny", deny: "QUOTING_UNSUPPORTED"},
		{name: "dollar-quoted-later-argument", input: "git commit -m $'x'", status: "deny", deny: "QUOTING_UNSUPPORTED"},
		{name: "unsupported-word", input: "ls <<<'x'", status: "deny", deny: "REDIRECTION_OP_UNSUPPORTED"},
		{name: "wrapper-command-interpreter", input: "command bash -c 'git commit -m fixture'", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "wrapper-builtin-eval", input: "builtin eval 'git commit -m fixture'", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "wrapper-command-git", input: "command git commit -m fixture", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "wrapper-env-git", input: "env git commit -m fixture", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "wrapper-env-split", input: "env -S bash -c 'git commit -m fixture'", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "trap-script", input: "trap 'git commit -m fixture' EXIT", status: "unsupported", deny: "HIDDEN_EXECUTION_UNSUPPORTED"},
		{name: "alias-definition", input: "alias save='git commit'", status: "unsupported", deny: "HIDDEN_EXECUTION_UNSUPPORTED"},
		{name: "unalias-redefinition", input: "alias save='git commit'\nunalias save", status: "unsupported", deny: "HIDDEN_EXECUTION_UNSUPPORTED"},
		{name: "shopt-alias-save", input: "shopt -s expand_aliases\nalias save='git commit'\nsave -m fixture", status: "unsupported", deny: "HIDDEN_EXECUTION_UNSUPPORTED"},
		{name: "function-forwarding", input: "g() { git \"$@\"; }; g commit -m fixture", status: "unsupported", deny: "FUNCTION_DECLARATION_UNSUPPORTED"},
		{name: "git-dynamic-subcommand", input: "git \"${verb:-commit}\" -m fixture", status: "unsupported", deny: "GIT_DYNAMIC_INVOCATION"},
		{name: "plain-dynamic-argument", input: "echo \"$x\"", status: "analyze", flags: []string{"DYNAMIC_WORDS"}},
		{name: "compound-arm", input: "true && git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "background-commit", input: "git commit -m fixture &", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "negated-commit", input: "! git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "redirected-commit", input: "git commit -m fixture >out", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "assignment-prefix-commit", input: "A=1 git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "two-statements", input: "true; git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "trailing-newline-simple", input: "git commit -m fixture\n", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "log-grep-commit", input: "git log --grep commit", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "git-substring-argument", input: "digit commit -m fixture", status: "analyze", flags: []string{}},
		{name: "rtk-git-commit", input: "rtk git commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "rtk-proxy-git-commit", input: "rtk proxy git commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "rtk-path-git-commit", input: "/usr/bin/rtk git commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "rtk-c-option-commit", input: "rtk git -C ../root commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "rtk-unknown-dispatch", input: "rtk \"$tool\" git commit -m fixture", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "rtk-then-not-git", input: "rtk proxy extra git commit -m x", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "rtk-proxy-dynamic-git", input: "rtk proxy git \"${verb:-commit}\" -m fixture", status: "unsupported", deny: "GIT_DYNAMIC_INVOCATION"},
		{name: "rtk-proxy-interpreter", input: "rtk proxy bash -c 'git commit -m fixture'", status: "unsupported", deny: "INTERPRETER_EXECUTOR"},
		{name: "rtk-proxy-hidden-trap", input: "rtk proxy trap 'git commit -m fixture' EXIT", status: "unsupported", deny: "HIDDEN_EXECUTION_UNSUPPORTED"},
		{name: "bare-proxy-git", input: "proxy git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "bare-proxy-plain", input: "proxy list", status: "analyze", flags: []string{}},
		{name: "bare-rtk-ordinary", input: "rtk list", status: "analyze", flags: []string{}},
		{name: "bare-rtk", input: "rtk", status: "analyze", flags: []string{}},
		{name: "nested-rtk-interpreter", input: "rtk proxy rtk proxy bash -c 'git commit -m fixture'", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "nested-rtk-dynamic-git", input: "rtk proxy rtk proxy git \"${verb:-commit}\" -m fixture", status: "unsupported", deny: "WRAPPER_DISPATCH_UNSUPPORTED"},
		{name: "glob-command-name", input: "g?t commit -m fixture", status: "unsupported", deny: "DYNAMIC_COMMAND_NAME"},
		{name: "glob-argument-commit", input: "git commit -m fixture/*", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "glob-question-argument-commit", input: "git commit -m f?xture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "glob-class-argument-commit", input: "git commit -m [f]ixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "tilde-root-commit", input: "git -C ~/repo commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED", "DYNAMIC_WORDS"}},
		{name: "tilde-command-name", input: "~/bin/git commit -m fixture", status: "unsupported", deny: "DYNAMIC_COMMAND_NAME"},
		{name: "escaped-glob-command-name", input: "g\\?t commit -m fixture", status: "analyze", flags: []string{}},
		{name: "quoted-glob-command-name", input: "\"g?t\" commit -m fixture", status: "analyze", flags: []string{}},
		{name: "quoted-glob-argument", input: "git commit -m \"fixture/*\"", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "single-quoted-glob-argument", input: "git commit -m 'fixture/*'", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "escaped-tilde-root", input: "git -C \\~/repo commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "quoted-tilde-root", input: "git -C \"~/repo\" commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "ordinary-glob-word", input: "echo *", status: "analyze", flags: []string{"DYNAMIC_WORDS"}},
		{name: "ordinary-glob-word-single-quoted", input: "echo \"*\"", status: "analyze", flags: []string{}},
		{name: "time-clause-ordinary", input: "time true", status: "analyze", flags: []string{}},
		{name: "time-wrapped-commit", input: "time git commit -m fixture", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
		{name: "rtk-git-commit", input: "rtk git commit -m fixture", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "quoted-empty-argument", input: "git commit -m \"\"", status: "analyze", flags: []string{"GIT_COMMIT_SIMPLE"}},
		{name: "two-c-prefixes", input: "git -C a -C b commit -m x", status: "analyze", flags: []string{"COMMIT_WORDS_DETECTED"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, _ := analyze(tc.input)
			assertResult(t, got, tc.status, tc.deny, tc.flags)
		})
	}
}

func TestAnalyzeBadInput(t *testing.T) {
	cases := []struct {
		name   string
		input  string
		status string
		deny   string
	}{
		{name: "parse-error", input: "git commit )", status: "deny", deny: "PARSE_INVALID"},
		{name: "unterminated-here-doc", input: "cat <<END\nbody", status: "deny", deny: "PARSE_INVALID"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, _ := analyze(tc.input)
			assertResult(t, got, tc.status, tc.deny, nil)
		})
	}
}

func TestJSONDecodeStrictness(t *testing.T) {
	sample := `{"schema": "shell-capture-parser@1", "command": "echo hello", "extra": 1}`
	decoder := json.NewDecoder(strings.NewReader(sample))
	decoder.DisallowUnknownFields()
	var request request
	if err := decoder.Decode(&request); err == nil {
		t.Fatal("expected unknown-field rejection")
	}
}

func TestAnalyzeResponseCarriesSchema(t *testing.T) {
	got, _ := analyze("echo hello")
	if got.Schema != schemaVersion {
		t.Fatalf("schema = %q, want %q", got.Schema, schemaVersion)
	}
	if got.Status != "analyze" {
		t.Fatalf("status = %q, want analyze", got.Status)
	}
	denied, _ := analyze("git commit )")
	if denied.Schema != schemaVersion {
		t.Fatalf("denied schema = %q, want %q", denied.Schema, schemaVersion)
	}
}

func TestTrailingJSONRejected(t *testing.T) {
	payload := `{"schema": "shell-capture-parser@1", "command": "echo hello"}{"schema": "shell-capture-parser@1", "command": "echo second"}`
	decoder := json.NewDecoder(strings.NewReader(payload))
	decoder.DisallowUnknownFields()
	var request request
	if err := decoder.Decode(&request); err != nil {
		t.Fatalf("first decode failed: %v", err)
	}
	if _, tailErr := decoder.Token(); tailErr != io.EOF {
		return
	}
	t.Fatal("expected trailing document rejection")
}

func TestDeepNestedParameters(t *testing.T) {
	inner := "echo x-value-marker"
	for index := 0; index < 200; index++ {
		inner = "${a:-" + inner + "}"
	}
	command := "git commit -m " + inner
	got, _ := analyze(command)
	if got.Status != "deny" || got.ReasonCode != "AST_LIMIT_EXCEEDED" {
		t.Fatalf("status = %q code = %q, want deny AST_LIMIT_EXCEEDED", got.Status, got.ReasonCode)
	}
	if strings.Contains(got.ReasonCode, "marker") {
		t.Fatal("raw input text leaked into the output")
	}
}

func TestDeepCommandSubstitution(t *testing.T) {
	inner := "echo x"
	for index := 0; index < 200; index++ {
		inner = "echo $(" + inner + ")"
	}
	got, _ := analyze(inner)
	if got.Status != "deny" || got.ReasonCode != "AST_LIMIT_EXCEEDED" {
		t.Fatalf("status = %q code = %q, want deny AST_LIMIT_EXCEEDED", got.Status, got.ReasonCode)
	}
}

func TestBroadNodeLimit(t *testing.T) {
	var builder strings.Builder
	for index := 0; index < 66000; index++ {
		builder.WriteString("a\n")
	}
	if builder.Len() > maxInputBytes {
		t.Fatalf("fixture exceeded the input bound: %d", builder.Len())
	}
	got, _ := analyze(builder.String())
	if got.Status != "deny" || got.ReasonCode != "AST_LIMIT_EXCEEDED" {
		t.Fatalf("status = %q code = %q, want deny AST_LIMIT_EXCEEDED", got.Status, got.ReasonCode)
	}
}

func TestMalformedDeepInput(t *testing.T) {
	inner := "echo"
	for index := 0; index < 300; index++ {
		inner = "(" + inner
	}
	got, _ := analyze(inner)
	if got.Status != "deny" || got.ReasonCode != "PARSE_INVALID" {
		t.Fatalf("status = %q code = %q, want deny PARSE_INVALID", got.Status, got.ReasonCode)
	}
}

func TestWatchdogSeamExpiredBudget(t *testing.T) {
	original := terminateProcess
	defer func() { terminateProcess = original }()
	called := make(chan int, 1)
	terminateProcess = func(code int) { called <- code }
	armWatchdog(time.Now().Add(-time.Hour))
	if code := <-called; code != watchdogCode {
		t.Fatalf("watchdog exited with %d, want %d", code, watchdogCode)
	}
}

func TestWatchdogSeamFiresForDueBudget(t *testing.T) {
	original := terminateProcess
	originalBudget := budget
	defer func() {
		terminateProcess = original
		budget = originalBudget
	}()
	called := make(chan int, 1)
	terminateProcess = func(code int) { called <- code }
	budget = 15 * time.Millisecond
	armWatchdog(time.Now())
	select {
	case code := <-called:
		if code != watchdogCode {
			t.Fatalf("watchdog exited with %d, want %d", code, watchdogCode)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("watchdog never fired for a due budget")
	}
}
