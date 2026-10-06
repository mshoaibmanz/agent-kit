# Rendered by agent-kit from roles.toml for host claude. Edit roles.toml, then render.
RV_ROLES_HOST=claude
RV_ROLES_PROVIDER=anthropic
RV_ROUND1='bug-reviewer cross-reviewer'
RV_LATER=cross-reviewer
RV_ONCE=quality-reviewer
RV_SENSITIVE_ADDS=bug-reviewer
# role provider model effort invoke prefix fallback timeout
RV_ROLE_TABLE='main anthropic opus[1m] high run - - 900
engineer anthropic claude-opus-5-5 high run - - 900
researcher anthropic sonnet medium run - - 900
web-browser anthropic sonnet medium run - - 900
second-opinion anthropic fable high run - - 900
task-reviewer anthropic opus high run R - 900
bug-reviewer anthropic inherit high run B - 900
quality-reviewer anthropic opus high run Q - 900
cross-reviewer openai gpt-6.1-sol high run CX bug-reviewer 900'
