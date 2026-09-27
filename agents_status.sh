#!/usr/bin/env bash
# AI 코딩 에이전트별 설치 여부와 현재 설정된 LLM 프로바이더를 조사하여 출력한다.
# 아무 것도 변경하지 않으며 조사 결과만 보여줌. 대상 도구 목록은 sync-instructions 와 동일함.
#
# 출력 형식
#   [ok]    도구이름: 실행파일 경로 | provider: 프로바이더
#   [cfg]   도구이름: 실행파일 없음, 설정 디렉토리만 남음 | provider: 프로바이더
#   [none]  도구이름: 미설치
set -uo pipefail

# 형식: "도구이름|실행파일후보(공백 구분)|최상위디렉토리|프로바이더조사함수"
TARGETS=(
  "claude|claude|$HOME/.claude|probe_claude"
  "codex cli|codex|$HOME/.codex|probe_codex"
  "commandcode|commandcode|$HOME/.commandcode|probe_commandcode"
  "copilot CLI|copilot|$HOME/.copilot|probe_copilot"
  "opencode|opencode|$HOME/.config/opencode|probe_opencode"
  "omp|omp|$HOME/.omp|probe_omp"
  "pi|pi|$HOME/.pi|probe_pi"
  "antigravity|agy antigravity gemini|$HOME/.gemini|probe_antigravity"
  "crush|crush|$HOME/.config/crush|probe_crush"
  "hermes|hermes|$HOME/.hermes|probe_hermes"
  "openclaw|openclaw|$HOME/.openclaw|probe_openclaw"
)

# JSON 파일에서 "key": "value" 형태의 첫 문자열 값을 꺼냄 (중첩 무시, 단순 검색)
json_str() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  grep -oE "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$file" | head -n1 | sed -E 's/^"[^"]*"[[:space:]]*:[[:space:]]*"([^"]*)"$/\1/'
}

# JSON 파일에서 "key": { 형태로 등장하는 최상위급 키 목록을 콤마로 이어 반환함
json_obj_keys() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  grep -oE '^[[:space:]]{2,4}"[a-zA-Z0-9_-]+"[[:space:]]*:[[:space:]]*\{' "$file" \
    | sed -E 's/^[[:space:]]*"([^"]*)".*/\1/' | paste -sd, -
}

# "provider/model" 문자열에서 프로바이더 부분만 꺼냄
provider_of() {
  local ref="$1"
  [[ "$ref" == */* ]] && printf '%s' "${ref%%/*}" || printf '%s' "$ref"
}

probe_claude() {
  local s="$HOME/.claude/settings.json"
  # 환경변수 또는 settings.json 의 env 블록에서 대체 백엔드 지정 여부 확인
  local src
  src="$(env; [[ -f "$s" ]] && cat "$s")"
  if grep -qE 'CLAUDE_CODE_USE_BEDROCK["=: ]+["]?(1|true)' <<<"$src"; then echo "anthropic (AWS Bedrock)"; return; fi
  if grep -qE 'CLAUDE_CODE_USE_VERTEX["=: ]+["]?(1|true)' <<<"$src"; then echo "anthropic (Google Vertex AI)"; return; fi
  if grep -qE 'CLAUDE_CODE_USE_FOUNDRY["=: ]+["]?(1|true)' <<<"$src"; then echo "anthropic (Azure AI Foundry)"; return; fi
  local base
  base="$(grep -oE 'ANTHROPIC_BASE_URL["=: ]+["]?[^" ,]+' <<<"$src" | head -n1 | sed -E 's/.*["=: ]+"?//')"
  if [[ -n "$base" ]]; then echo "anthropic 호환 ($base)"; return; fi
  echo "anthropic"
}

probe_codex() {
  local cfg="$HOME/.codex/config.toml" auth="$HOME/.codex/auth.json"
  local mp
  mp="$([[ -f "$cfg" ]] && grep -E '^model_provider[[:space:]]*=' "$cfg" | head -n1 | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/')"
  if [[ -n "$mp" ]]; then echo "$mp"; return; fi
  local mode
  mode="$(json_str "$auth" auth_mode)"
  case "$mode" in
    chatgpt) echo "openai (ChatGPT 로그인)" ;;
    apikey)  echo "openai (API key)" ;;
    "")      echo "openai (미로그인)" ;;
    *)       echo "openai ($mode)" ;;
  esac
}

probe_commandcode() {
  local cfg="$HOME/.commandcode/config.json"
  local mp model
  mp="$(json_str "$cfg" provider)"
  [[ -z "$mp" ]] && mp="$(json_str "$cfg" modelProvider)"
  if [[ -z "$mp" ]]; then
    model="$(json_str "$cfg" model)"
    [[ "$model" == ?*/?* ]] && mp="$(provider_of "$model")"
  fi
  echo "${mp:-unknown}"
}

probe_copilot() {
  echo "github-copilot"
}

probe_opencode() {
  local cfg model
  for cfg in "$HOME/.config/opencode/opencode.jsonc" "$HOME/.config/opencode/opencode.json"; do
    model="$(json_str "$cfg" model)"
    [[ -n "$model" ]] && { provider_of "$model"; echo; return; }
  done
  # 설정에 model 이 없으면 최근 사용 모델(state/model.json)의 providerID 사용
  local state="${XDG_STATE_HOME:-$HOME/.local/state}/opencode/model.json"
  local pid
  pid="$(json_str "$state" providerID)"
  if [[ -n "$pid" ]]; then echo "$pid (최근 사용)"; return; fi
  echo "unknown"
}

probe_omp() {
  local cfg="$HOME/.omp/agent/config.yml"
  [[ -f "$cfg" ]] || { echo "unknown"; return; }
  local ref
  ref="$(awk '/^modelRoles:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&/^[[:space:]]+default:/{sub(/^[[:space:]]+default:[[:space:]]*/,""); print; exit}' "$cfg")"
  [[ -n "$ref" ]] && provider_of "$ref" || echo "unknown"
}

probe_pi() {
  local cfg="$HOME/.pi/agent/settings.json" auth="$HOME/.pi/agent/auth.json"
  local p
  p="$(json_str "$cfg" defaultProvider)"
  if [[ -n "$p" ]]; then echo "$p"; return; fi
  # 단일 인증은 자동 선택 후보로 추정하며 복수 인증은 확정하지 않음
  local keys
  keys="$(json_obj_keys "$auth")"
  case "$keys" in
    "") echo "unknown" ;;
    *,*) echo "자동 선택 (인증 후보: $keys)" ;;
    *) echo "$keys (인증 기반 추정)" ;;
  esac
}

probe_antigravity() {
  echo "google"
}

probe_crush() {
  local cfg="${XDG_DATA_HOME:-$HOME/.local/share}/crush/crush.json"
  [[ -f "$cfg" ]] || { echo "unknown"; return; }
  # models.large.provider 를 우선 사용. 단순 검색이므로 "large" 블록 이후 첫 provider 를 취함
  local p
  p="$(tr -d '\n' <"$cfg" | grep -oE '"large"[[:space:]]*:[[:space:]]*\{[^}]*\}' | head -n1 | grep -oE '"provider"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"([^"]*)"$/\1/')"
  echo "${p:-unknown}"
}

probe_hermes() {
  local cfg="$HOME/.hermes/config.yaml"
  [[ -f "$cfg" ]] || { echo "unknown"; return; }
  local p
  p="$(awk '/^model:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&/^[[:space:]]+provider:/{sub(/^[[:space:]]+provider:[[:space:]]*/,""); print; exit}' "$cfg")"
  echo "${p:-unknown}"
}

probe_openclaw() {
  local cfg="$HOME/.openclaw/openclaw.json"
  local ref
  ref="$(json_str "$cfg" primary)"
  # model 객체의 primary 가 없으면 문자열형 model 을 사용함.
  [[ -z "$ref" ]] && ref="$(json_str "$cfg" model)"
  [[ -n "$ref" ]] && provider_of "$ref" || echo "unknown"
}

installed=0
cfg_only=0
missing=0

for entry in "${TARGETS[@]}"; do
  IFS='|' read -r name bins root probe <<<"$entry"

  bin_path=""
  for b in $bins; do
    if bin_path="$(command -v "$b" 2>/dev/null)"; then break; fi
    bin_path=""
  done

  if [[ -n "$bin_path" ]]; then
    printf '[ok]    %s: %s | provider: %s\n' "$name" "$bin_path" "$("$probe")"
    installed=$((installed + 1))
  elif [[ -d "$root" ]]; then
    printf '[cfg]   %s: 실행파일 없음, 설정 디렉토리만 남음 (%s) | provider: %s\n' "$name" "$root" "$("$probe")"
    cfg_only=$((cfg_only + 1))
  else
    printf '[none]  %s: 미설치\n' "$name"
    missing=$((missing + 1))
  fi
done

echo
echo "합계: 설치 ${installed}개, 설정만 남음 ${cfg_only}개, 미설치 ${missing}개"
