# Rendered by agent-kit from roles.toml for host claude. Edit roles.toml, then render.
RV_ROLES_HOST=claude
RV_ROLES_PROVIDER=anthropic
RV_ROUND1='thermo-bugs review-cross'
RV_LATER=review-cross
RV_ONCE=thermo-quality
RV_SENSITIVE_ADDS=thermo-bugs
# role provider model effort invoke prefix fallback timeout
RV_ROLE_TABLE='main anthropic opus[1m] high run - - 900
worker anthropic opus high run - - 900
scout anthropic sonnet medium run - - 900
adversary anthropic fable high run - - 900
reviewer anthropic opus high run R - 900
thermo-bugs anthropic inherit high run B - 900
thermo-quality anthropic opus high run Q - 900
review-cross openai gpt-6.1-sol high run CX thermo-bugs 900'
