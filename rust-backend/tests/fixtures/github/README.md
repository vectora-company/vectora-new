# Fixtures do GitHub (saídas reais do `gh`)

Os arquivos `gh_*` desta pasta são saídas **reais** do `gh`, capturadas neste
repositório (`vectora-company/vectora`) só com comandos de leitura, e usadas
como base das simulações dos testes do backend, do frontend e do e2e. Nenhum
teste inventa um formato do GitHub: ele parte do que o `gh` devolveu.

| Arquivo                                     | Comando de origem                                                                                                                                               |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gh_run_list.json`                          | `gh run list --limit 3 --json databaseId,number,displayTitle,workflowName,status,conclusion,event,headBranch,headSha,createdAt,updatedAt,startedAt,url,attempt` |
| `gh_run_view.json`                          | `gh run view <id> --json <os campos acima>,jobs`                                                                                                                |
| `gh_run_view_job_log.txt`                   | `gh run view <id> --job <job> --log` (primeiras 25 linhas)                                                                                                      |
| `gh_workflow_list.json`                     | `gh workflow list --json id,name,state` (3 primeiros)                                                                                                           |
| `gh_pr_list.json` / `gh_pr_view.json`       | `gh pr list` / `gh pr view <n>` com os campos que o handler pede                                                                                                |
| `gh_issue_list.json` / `gh_issue_view.json` | `gh issue list` / `gh issue view <n>` com os campos que o handler pede                                                                                          |

Os campos pedidos estão em `backend/api/handlers/github.py` (`_RUN_LIST_FIELDS`,
`_PR_DETAIL_FIELDS` etc.).

## Privacidade

As fixtures não podem ter e-mail de pessoa: o `gh pr view` devolve o e-mail do
autor dos commits, que é removido (junto de `reactionGroups` e de campos
internos) antes de salvar. `test_github_real_fixtures.py` reprova qualquer
e-mail nas fixtures.

## Respostas do backend (`frontend/e2e/fixtures/github`)

O frontend e o Playwright não usam o formato do `gh`, e sim o que o backend
responde. Esses arquivos são exatamente a saída dos handlers para as fixtures
`gh_*` desta pasta. O teste `test_frontend_fixtures_match_what_the_backend_produces`
compara os dois: se o handler mudar, o teste aponta qual arquivo regenerar.

## Casos reais que as fixtures cobrem

- run **em andamento**, com `conclusion` vazia;
- jobs **pulados** (`skipped`) sem nenhum passo;
- log com BOM e cada linha prefixada por `job<TAB>passo<TAB>hora`;
- id de run acima de 32 bits;
- PR com milestone, comentário, checks e mergeabilidade.

O que o repositório só produz quando o CI quebra (job com falha) é coberto por
uma variante mínima dentro dos testes: o mesmo run real com um job marcado como
falho.

## Teste com o `gh` de verdade

`tests/unit/test_github_actions_live.py` (marker `live`, fora da suíte padrão)
chama o `gh` real, somente leitura e somente neste repositório. Um guarda reprova
qualquer subcomando fora de `run list|view`, `workflow list`, `pr list|view` e
`issue list|view`, e qualquer `cwd` fora da raiz do checkout. Sem `gh`, sem login
ou em outro repositório, o teste é pulado.
