#!/usr/bin/env bash
# Claude Code status line: model | dir | branch | ctx used% + tokens | 5h/7d rate limits.
# ONE jq call and one git call per render (the previous version forked jq seven times plus bc).
input=$(cat)
IFS=$'\t' read -r model cwd used_pct used_tok window five_hr seven_day < <(printf '%s' "$input" | jq -r '
  [ (.model.display_name // "unknown"),
    (.cwd // .workspace.current_dir // ""),
    ((.context_window.used_percentage // 0) | floor),
    (.context_window.current_usage.input_tokens // 0),
    (.context_window.context_window_size // 0),
    ((.rate_limits.five_hour.used_percentage // -1) | floor),
    ((.rate_limits.seven_day.used_percentage // -1) | floor) ] | @tsv' 2>/dev/null)

R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'; CY=$'\033[36m'; YE=$'\033[33m'; GR=$'\033[32m'; RD=$'\033[31m'; BL=$'\033[34m'; MG=$'\033[35m'
sep="${D} | ${R}"
out="${CY}${B}${model}${R}${sep}${BL}$(basename "${cwd:-.}")${R}"

branch=$(GIT_OPTIONAL_LOCKS=0 git -C "${cwd:-.}" symbolic-ref --short HEAD 2>/dev/null)
[ -n "$branch" ] && out="${out}${sep}${MG}${branch}${R}"

if [ "${window:-0}" -gt 0 ]; then
  c="$GR"; [ "${used_pct:-0}" -gt 50 ] && c="$YE"; [ "${used_pct:-0}" -gt 80 ] && c="$RD"
  out="${out}${sep}${D}ctx:${R}${c}${used_pct}%${R}${D} $((used_tok / 1000))K/$((window / 1000))K${R}"
fi

rate=""
[ "${five_hr:--1}" -ge 0 ] && rate="5h:${five_hr}%"
[ "${seven_day:--1}" -ge 0 ] && rate="${rate:+$rate }7d:${seven_day}%"
[ -n "$rate" ] && out="${out}${sep}${D}${rate}${R}"

printf '%s\n' "$out"
