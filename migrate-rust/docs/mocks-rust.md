# Mocks Rust para a migração do Vectora

A meta é reconstruir os mocks por contrato antes de portar os testes consumidores. O índice de cobertura deve ligar cada uso Python ao fake Rust correspondente e registrar o que falta: resultado, erro, ordem, parâmetros, contagem, awaits, estado, isolamento e teardown. A repetição do mesmo mock em dezenas de testes deve reutilizar a mesma implementação.

## O que a Engine mostra

No checkout e61e180, a Engine usa asserções nativas de Rust e Tokio para testes assíncronos. O teste de transcrição injeta um MemoryStore que implementa MediaStore. Os testes de conformidade compartilham fixtures JSON com os bindings e usam um servidor loopback que captura a requisição e devolve a resposta roteirizada. O Cargo.toml do crate não declara mockall ou wiremock. Esses exemplos sustentam o uso inicial de fakes tipados, serde_json para fixtures compartilhadas e Tokio nos consumidores assíncronos. O crate de fakes começou com a biblioteca padrão. Passou a usar `serde_json` para corpos e linhas JSON e `rusqlite` (SQLite embutido) para as fixtures `db`, que dependem de SQL real.

## Inventário inicial

A busca estática nos arquivos test_*.py encontrou 2.436 chamadas de monkeypatch.setattr, 1.460 construções com nome AsyncMock, 1.108 chamadas com nome patch e 701 construções com nome MagicMock. Há também 243 setenv, 118 delenv, 93 assert_not_called e 91 assert_not_awaited. São ocorrências sintáticas. Aliases, factories indiretas, fixtures em conftest.py e fakes definidos como classes exigem inventário complementar antes de declarar 100%.

| Família                                    | Decisão                                                                                              | Estado                                                                                                                              |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Mock, MagicMock e AsyncMock                | Scripts tipados de valores e erros, com histórico de argumentos e awaits                             | Base implementada. Comportamentos mágicos, encadeamento e side effects que executam código ainda precisam de contratos específicos. |
| Factory e readiness de provider            | Script para a factory injetada e probe independente para capabilities e health                       | Probe implementado. Falta ligar ao trait do backend Rust quando o contrato for portado.                                             |
| Patch de configurações, ambiente e módulos | Passar configuração e dependências por instância. Para ambiente real, isolar no subprocesso do teste | Ambiente por instância implementado. Injeção nos consumidores pendente.                                                             |
| HTTP de entrada e saída                    | Adaptador conforme o contrato: serviço em processo para a API e loopback com registro para o cliente | Cliente tipado implementado. Adapters HTTP pendentes.                                                                               |
| Processos, relógio e filesystem            | Entrada injetada para subprocesso e relógio; diretórios temporários e limpeza por escopo             | Comandos e relógio virtual implementados. Lifecycle e filesystem pendentes.                                                         |
| Storage, browser e streaming               | Implementações de traits por capacidade, com estado e erros roteirizados                             | KV em memória implementado. Adapters de storage, browser e streaming pendentes.                                                     |

## Critério de cobertura

Um mock conta como reconstruído quando existe uma correspondência explícita para o contrato Python, o fake Rust permite observar as mesmas interações e o consumidor usa a implementação compartilhada. Um wrapper genérico, mock vazio ou teste ignorado não aumenta essa cobertura. O Depyler referencia o crate por uma única dependência de teste no Cargo.toml gerado. Ele procura o diretório mocks vizinho ao dist ou aceita --mocks. O crate manual fica fora da árvore de saída. Os mapeamentos de chamadas ainda precisam ser ligados aos consumidores.

## Famílias por consumo (revisão Python b526443)

Fixtures com mais testes consumidores, contados por parâmetro de teste: `db` de background (67), `trusted_ws` (53), `client` de provider routing (52), `ctx` (51) e `session_store` (50) do loop de conversa, `db` do kanban e `store` de sessões (49 cada), `client` de auth (41), `fake_page` (31).

| Família                             | Fake Rust           | Ligado ao gerador                 |
| ----------------------------------- | ------------------- | --------------------------------- |
| Processo e `create_subprocess_exec` | `subprocess`        | não                               |
| `httpx.AsyncClient` com contexto    | `http_session`      | não                               |
| `_FakePage` do browser              | `browser`           | sim, 24 de 31 consumidores        |
| Fixtures `db` com SQLite            | `database`          | sim, 17 de 49 no modelo do kanban |
| `trusted_ws`                        | `workspace`         | não                               |
| `client` do provider routing        | `client`            | sim, 21 de 52 consumidores        |
| `tmp_path`                          | `fixtures::TempDir` | sim, assinatura restrita          |

Ainda sem fake: os demais `client` da suíte, `session_store` e `ctx` (dependem de tipos do backend Rust que não existem), `capsys`, `caplog`, `patch.dict`, streaming e as demais classes fake locais.

Medição de `tmp_path` em 2026-11-25: candidatos de 548 para 657, com os mesmos 2 alvos e 3 candidatos compilando antes e depois, e os 3 falhando na execução. A regra ainda não trouxe nenhum teste verificável.

## Primeira ligação verificada

O Depyler (`vectora_fakes.rs`) traduz consumidores de `fake_page` somente quando todas as instruções são suportadas e quando a definição de `_FakePage` coincide com o contrato do fake. A chamada ao backend passa por `backend::Backend`, que falha com `TDD pending: capability ... is not implemented` até existir implementação Rust. Em `unit/test_tools_browser.py`, 24 testes compilam e falham por esse motivo. A paridade segue sem comprovação: as asserções só serão exercitadas quando a capacidade for implementada, e os argumentos e resultados em JSON são um contrato provisório.

A regra cobre também `AsyncMock` locais e instalados com `monkeypatch.setattr`, `SimpleNamespace` (inclusive mocks aninhados), `lambda _x: objeto`, troca de `side_effect` entre chamadas e arquivos sob `tmp_path`. As chamadas aos mocks guardam argumentos posicionais e nomeados (`arguments::Arguments`). Um `AsyncMock()` sem `return_value` devolve nulo, enquanto o Python devolveria um `MagicMock`.

Ficam pendentes 7 consumidores: dois da guarda de navegação e um da falha de navegação anterior (handlers de rota), dois de redirecionamento e um de preenchimento parcial (funções locais como `side_effect`) e o de screenshot (assets reais do backend).

## Fixtures `db`

A regra reconhece a fixture pela definição: arquivo sob `tmp_path`, esquema SQL literal, `_get_db` substituído e `yield` da conexão seguido de `close`. Auxiliares do módulo feitos de `execute`, `commit` e `return` são expandidos com os argumentos literais de cada chamada. O teste traduzido cria o `SqliteDb`, entrega o banco ao `Backend` e consulta linhas por nome de coluna.

Medição em 2026-11-25 sobre `unit/test_kanban_model.py`: 16 dos 49 consumidores compilam e falham com `TDD pending`. A semeadura SQL roda antes da falha. Ficam fora os consumidores com outras fixtures ou instruções sem regra, e as variantes que leem o esquema de `_SCHEMA` no repositório Python, como a de `test_services_background.py`.

## Fixture `client`

A regra reconhece o par `client`/`app` pela definição: `TestClient(app, raise_server_exceptions=False)` sobre a aplicação criada com autenticação desligada. Cada requisição vira a capacidade `api VERBO /caminho`, com a URL completa como primeiro argumento. A resposta provisória é `{"status_code", "json", "text"}`. A regra cobre também `monkeypatch.setenv`/`delenv`, leituras de `os.environ`, fixtures que só limpam ambiente e settings, `patch(..., return_value=...)` e o bloco que substitui `httpx.AsyncClient`.

O banco SQLite isolado que a fixture `app` instala em `threads._get_db` **não é reproduzido**: a implementação Rust da API terá de receber um armazenamento isolado por teste. No Python a fixture tem escopo de módulo; em Rust cada teste recebe um backend novo.

Medição em 2026-11-26: 21 dos 52 consumidores compilam e falham com `TDD pending`.
