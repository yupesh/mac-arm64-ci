#!/bin/bash
# Build and smoke the M1 agent on a macOS arm64 runner.
# Sources come from the private GitLab with $GITLAB_TOKEN; the public log shows step names and results only,
# full logs go to logs/ and leave the runner encrypted.
set -uo pipefail
: "${GITLAB_TOKEN:?}" "${REF:?}" "${M1_ENV:=user}" "${RUN_PYTEST:=false}" "${TESTS:=}" "${VPN:=false}"
ROOT=$PWD; LOGS=$ROOT/logs; SRC=$ROOT/src; mkdir -p "$LOGS"
REPO=https://oauth2@gitlab.deeppavlov.ai/deeppavlov/deus-ex-harness-team/deus-ex-harness.git
export GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=$ROOT/.askpass
printf '#!/bin/sh\necho "$GITLAB_TOKEN"\n' >"$GIT_ASKPASS"; chmod 700 "$GIT_ASKPASS"
export PATH="$HOME/.local/bin:$PATH" M1_BUILD_ENVIRONMENT=$M1_ENV
FAILED=0

step() {  # step <name> <command…>: run quietly, print only the outcome
  local name=$1 t0=$SECONDS; shift
  if (cd "$SRC" 2>/dev/null || cd "$ROOT"; "$@") >"$LOGS/$name.log" 2>&1; then
    echo "PASS $name ($((SECONDS - t0)) s)"
  else
    echo "FAIL $name ($((SECONDS - t0)) s)"; FAILED=1; return 1
  fi
}

fetch() {
  git init -q "$SRC" && cd "$SRC" && git remote add origin "$REPO" &&
    git -c filter.lfs.process= -c filter.lfs.smudge= -c filter.lfs.required=false fetch -q --depth 1 origin "$REF" &&
    git -c filter.lfs.process= -c filter.lfs.smudge= -c filter.lfs.required=false checkout -q FETCH_HEAD &&
    git rev-parse HEAD
}
tools() { command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh; npm install -g -s pnpm@10.34.5 && pnpm --version; }
deps() { uv sync --all-groups --frozen && pnpm --dir app/desktop install --frozen-lockfile; }
build() {
  pnpm --dir app/desktop web:agent:build --target-arch arm64 &&
    test "$(lipo -archs app/desktop/.build/web-agent/dist/m1-agent)" = arm64
}
AGENT=$SRC/app/desktop/.build/web-agent/dist/m1-agent
artifact_smoke() { uv run python scripts/smoke_m1_artifact.py --artifact "$AGENT"; }
health() { python3 "$ROOT/smoke.py" "$AGENT"; }
ui() { cd "$LOGS" && node "$ROOT/ui.mjs" "$AGENT"; }
tests() { uv run pytest -q -p no:cacheprovider; }
targeted() { read -ra t <<<"$TESTS"; mkdir -p "$LOGS/stacks"; STACK_DUMP_DIR="$LOGS/stacks" PYTHONPATH="$ROOT/diag" uv run pytest -rA -p no:cacheprovider --basetemp="$LOGS/tmp" "${t[@]}"; }

export LOGS
if [ "$VPN" = true ]; then
  step vpn "$ROOT/vpn.sh" up && step lab "$ROOT/vpn.sh" probe
fi
if [ -n "$TESTS" ]; then
  step fetch fetch && echo "commit $(tail -1 "$LOGS/fetch.log")" && step tools tools &&
    step deps uv sync --all-groups --frozen &&
    step pytest targeted; echo "pytest: $(grep -E '[0-9]+ (passed|failed)' "$LOGS/pytest.log" | tail -1)"
  rm -f "$GIT_ASKPASS"; exit $FAILED
fi
if [ "${1:-}" = "stage-build" ]; then
  [ "$M1_ENV" = stage ] && step fetch fetch || exit 1
  actual=$(tail -1 "$LOGS/fetch.log")
  [ "$actual" = "$REF" ] || { echo "Unexpected stage commit"; exit 1; }
  echo "commit $actual"
  unset GITLAB_TOKEN
  rm -f "$GIT_ASKPASS"
  step tools tools && step deps deps && step build build
  exit $?
fi
step fetch fetch && echo "commit $(tail -1 "$LOGS/fetch.log")" &&
  step tools tools && step deps deps && step build build &&
  { step artifact_smoke artifact_smoke; step health health; step ui ui; }
if [ "$RUN_PYTEST" = true ] && [ -d "$SRC/.venv" ]; then
  step pytest tests; echo "pytest: $(grep -E '[0-9]+ (passed|failed)' "$LOGS/pytest.log" | tail -1)"
fi
rm -f "$GIT_ASKPASS"
exit $FAILED
