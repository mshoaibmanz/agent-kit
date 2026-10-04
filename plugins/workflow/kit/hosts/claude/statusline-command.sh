#!/usr/bin/env bash
# Claude Code status line: model | dir | branch | ctx used% + tokens | session cost | 5h/7d rate limits.
# ONE jq call and one git call per render (the previous version forked jq seven times plus bc).
# Tokens are what the context holds: input + cache creation + cache read of the last call
# (current_usage.input_tokens alone is the uncached tail, a few hundred tokens). Colour is absolute:
# yellow from 450K, red from CONTEXT_HANDOFF_AT (kit.env, default 600K: context-watch's handoff
# point); the old percentage steps stay for windows too small to reach those.
input=$(cat)
# The prompt cache (prompt_cache, Claude Code 2.1.251+): `cache ● 47m` while warm, green above a
# quarter of the TTL left, yellow at or under it (m:ss under 5 minutes); red `cache ○ cold` once it
# expired, with what re-warming costs: total_input_tokens x the model's cache-write $/MTok for that
# TTL (an unknown model shows no $). warm with expires_at already past reads as cold (a refresh can
# land before the next API response updates warm). Nothing before the first response or with no
# caching seen.
# STATUSLINE_NOW (epoch seconds) pins the clock for tests. refreshInterval in settings ticks it idle.
# Tab is IFS whitespace, so an empty field would merge into the next: every field is non-empty.
IFS=$'\t' read -r model cwd used_pct used_tok window cost five_hr seven_day cache_c cache_t < <(printf '%s' "$input" | jq -r '
  def pad2: tostring | if length < 2 then "0" + . else . end;
  (.context_window.current_usage // {}) as $u
  | (if ($u | length) > 0
       then (($u.input_tokens // 0) + ($u.cache_creation_input_tokens // 0) + ($u.cache_read_input_tokens // 0))
       else (.context_window.total_input_tokens // 0) end) as $tok
  | (.prompt_cache // null) as $pc
  | ((($ENV.STATUSLINE_NOW // "") | tonumber?) // now) as $now
  | (if $pc == null or $pc.caching_observed != true then ["-", "-"]
     elif $pc.warm == true and ($pc.expires_at == null or $pc.expires_at > $now) then
       (if $pc.expires_at == null then ["G", "cache ●"]
        else ([($pc.expires_at - $now), 0] | max | floor) as $r
          | (if $pc.ttl == "5m" then 300 else 3600 end) as $ttl
          | [ (if $r > $ttl / 4 then "G" else "Y" end),
              ("cache ● " + (if $r >= 300 then "\($r / 60 | floor)m" else "\($r / 60 | floor):\($r % 60 | pad2)" end)) ]
        end)
     else
       ((.model.id // "") as $id
        | (if $pc.ttl == "5m" then {o: 5, s: 2.5, h: 1.25, f: 12.5} else {o: 8, s: 4, h: 2, f: 20} end) as $p
        | (if ($id | test("opus-5-5")) then $p.o elif ($id | test("sonnet-5-5")) then $p.s
           elif ($id | test("haiku-4-5")) then $p.h elif ($id | test("fable-5-1")) then $p.f else null end) as $price
        | ((.context_window.total_input_tokens // $tok) as $in
           | if $price == null then ["R", "cache ○ cold"]
             else (($in * $price / 10000) | round) as $cents
               | ["R", "cache ○ cold · $\($cents / 100 | floor).\($cents % 100 | pad2) to rewarm"] end))
     end) as $cache
  | [ (.model.display_name // "unknown"),
    (.cwd // .workspace.current_dir // "" | if . == "" then "." else . end),
    ((.context_window.used_percentage // 0) | floor),
    $tok,
    (.context_window.context_window_size // 0),
    (.cost.total_cost_usd // -1),
    ((.rate_limits.five_hour.used_percentage // -1) | floor),
    ((.rate_limits.seven_day.used_percentage // -1) | floor),
    $cache[0], $cache[1] ] | @tsv' 2>/dev/null)

red_at=600000
{ . "$HOME/.claude/hooks/lib/hook-io" && kit_env; } 2>/dev/null
case ${CONTEXT_HANDOFF_AT:-} in '' | 0 | *[!0-9]*) ;; *) red_at=$CONTEXT_HANDOFF_AT ;; esac
yellow_at=450000
[ "$yellow_at" -lt "$red_at" ] || yellow_at=$((red_at * 3 / 4))

R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'; CY=$'\033[36m'; YE=$'\033[33m'; GR=$'\033[32m'; RD=$'\033[31m'; BL=$'\033[34m'; MG=$'\033[35m'
sep="${D} | ${R}"
out="${CY}${B}${model}${R}${sep}${BL}$(basename "${cwd:-.}")${R}"
# Which account (bin/claude-account): shown only for a non-personal config dir.
case "${CLAUDE_CONFIG_DIR:-}" in
  "$HOME"/.claude-*) out="${YE}${B}${CLAUDE_CONFIG_DIR##*/.claude-}${R}${sep}${out}" ;;
esac

branch=$(GIT_OPTIONAL_LOCKS=0 git -C "${cwd:-.}" symbolic-ref --short HEAD 2>/dev/null)
[ -n "$branch" ] && out="${out}${sep}${MG}${branch}${R}"

if [ "${window:-0}" -gt 0 ]; then
  tok=${used_tok:-0}
  c="$GR"
  { [ "$tok" -ge "$yellow_at" ] || [ "${used_pct:-0}" -gt 50 ]; } && c="$YE"
  { [ "$tok" -ge "$red_at" ] || [ "${used_pct:-0}" -gt 80 ]; } && c="$RD"
  out="${out}${sep}${D}ctx:${R}${c}${used_pct}%${R}${D} $((tok / 1000))K/$((window / 1000))K${R}"
fi

case ${cache_c:--} in
  G) out="${out}${sep}${GR}${cache_t}${R}" ;;
  Y) out="${out}${sep}${YE}${cache_t}${R}" ;;
  R) out="${out}${sep}${RD}${cache_t}${R}" ;;
esac

case ${cost:--1} in -*) ;; *) out="${out}${sep}${D}\$$(printf '%.2f' "$cost")${R}" ;; esac

rate=""
[ "${five_hr:--1}" -ge 0 ] && rate="5h:${five_hr}%"
[ "${seven_day:--1}" -ge 0 ] && rate="${rate:+$rate }7d:${seven_day}%"
[ -n "$rate" ] && out="${out}${sep}${D}${rate}${R}"

printf '%s\n' "$out"
