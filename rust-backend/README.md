# Vectora backend (Rust)

Backend do Vectora em Rust, traduzido do backend Python. Cada arquivo de
`backend/` tem um módulo correspondente em `src/`, e cada arquivo de teste da
suíte Python tem um teste em `tests/`.

Este diretório é gerado pelo `migrate-rust`. Não edite os arquivos gerados
aqui: a correção vai para os mapas, os patterns, o runtime ou os mocks do
`migrate-rust`, e a próxima geração a traz para cá.

## Estrutura

| Caminho                | Conteúdo                                                      |
| ---------------------- | ------------------------------------------------------------- |
| `src/`                 | Módulos do backend, um por arquivo Python                     |
| `src/bin/`             | Binário do servidor                                           |
| `assets/`              | Arquivos de dados do backend: contratos, migrations, cenários |
| `crates/runtime/`      | Runtime do valor dinâmico usado pelo código traduzido         |
| `crates/mocks/`        | Dublês de teste e fixtures escritos à mão                     |
| `crates/test-support/` | Tabela de nomes compartilhada pelos testes                    |
| `tests/`               | Suíte traduzida: `unit/`, `integration/`, `e2e/`, `stress/`   |

## TDD

Os testes chamam as capacidades do backend. Enquanto uma capacidade ainda não
foi traduzida, o teste falha com `TDD pending: capability ...`, que é o vermelho
esperado. Qualquer outra falha é um problema de migração.

## Comandos

```bash
cargo build
```

```bash
cargo test --test m006_test_frontend_served_lifecycle
```

Compilar todos os testes de uma vez exige bastante memória; prefira limitar os
processos com `--jobs`.

## Licença

AGPL-3.0-only.

## Hooks

Como repositório próprio, instale os hooks com `pre-commit install`. Eles
verificam formatação, clippy e dependências sem uso, e exigem
`cargo install cargo-machete`.
