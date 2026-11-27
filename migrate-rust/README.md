# migrate-rust

Migração do backend do Vectora de Python para Rust, por mapas e patterns. Um mapa é uma tabela de trocas diretas de um assunto (`def` → `fn`, `int` → `i64`). Um pattern é a regra que reconhece cada situação e escolhe os mapas a aplicar. O motor percorre cada arquivo do backend e da suíte de testes, aplica o pattern certo e deixa uma pendência localizada onde ainda falta regra.

A saída, `rust-backend/`, é o repositório do backend Rust: um app Cargo completo, com README, `.gitignore`, crates, módulos do backend e testes.

## Estado

| Parte      | Situação                                                                                                                          |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Testes     | 503 arquivos traduzidos em `rust-backend/tests/`. Na última medição, 7457 de 7975 casos chegam ao `TDD pending` e 81 passam.      |
| Backend    | Esqueleto gerado: um módulo vazio em `rust-backend/src/` por arquivo de `backend/`. A tradução do código começa na próxima etapa. |
| `reports/` | `report.json` (pendências por forma), `cargo-check.json` (compilação e execução) e `unresolved-patterns.log`.                     |

## Estrutura

```text
src/                motor: percorre a árvore, aplica patterns e mapas, monta rust-backend/
maps/               mapas, patterns e contratos de mocks
runtime/            crate manual vectora-runtime: valor dinâmico, operadores, builtins e módulos
mocks/              crate manual vectora-test-mocks: dublês de teste e fixtures
template/           arquivos fixos do app gerado (README, .gitignore, toolchain, binário)
tools/              check_targets.py (compila e executa) e measure_mocks.py (inventário)
reports/            relatórios versionados de cada etapa
docs/               épico e decisões sobre mocks
../rust-backend/    SAÍDA: o repositório do backend Rust
```

`rust-backend/` é regenerado por inteiro a cada geração. `runtime/` e `mocks/` são copiados para `rust-backend/crates/` sem alteração; a geração só os lê.

As regras de trabalho estão em [CLAUDE.md](CLAUDE.md), com cópia idêntica em [AGENTS.md](AGENTS.md).

## Verificação

O `.pre-commit-config.yaml` da raiz do repositório tem hooks que rodam só em `migrate-rust/` e `rust-backend/`: geração em dia, rustfmt, clippy, testes dos mocks e dependências sem uso.

```bash
cargo +stable test --manifest-path mocks/Cargo.toml --target-dir target/mocks
```

```bash
cargo +stable run
```

```bash
python3 tools/check_targets.py --run
```

`--run` precisa do `bwrap` e executa cada binário sem rede, com `HOME` temporário.
