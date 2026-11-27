# Instruções de trabalho — migrate-rust

Este repositório migra o backend do Vectora e sua suíte de testes de Python para Rust por **mapas de troca direta** aplicados por **patterns**. Seguimos TDD: os testes traduzidos chamam as capacidades do backend Rust e falham pelo motivo esperado até cada capacidade ser traduzida.

A saída, `rust-backend/`, é o repositório independente do backend Rust: um workspace Cargo com README, `.gitignore`, `rust-toolchain.toml`, `src/`, `crates/` e `tests/`. Ele é regenerado por inteiro; nunca edite arquivos dentro de `rust-backend/`.

## Checkouts

| Finalidade                                             | Caminho                             |
| ------------------------------------------------------ | ----------------------------------- |
| Este repositório: motor, mapas, runtime, mocks e saída | `vectora-rust/migrate-rust`         |
| Backend e testes Python de referência                  | `vectora-rust/{backend,tests}`      |
| Engine Rust, só como referência de contratos           | `/mnt/externo/vectora-repos/engine` |

Antes de editar, confira `git status` e preserve mudanças locais. O backend Python e a Engine são somente leitura para este trabalho.

## Estrutura

| Caminho         | Conteúdo                                                                                |
| --------------- | --------------------------------------------------------------------------------------- |
| `src/`          | Motor: percorre a árvore, aplica patterns e mapas e monta `rust-backend/` (`layout.rs`) |
| `maps/`         | Mapas, patterns e contratos de mocks                                                    |
| `runtime/`      | Crate manual `vectora-runtime`: valor dinâmico, operadores, builtins e módulos mapeados |
| `mocks/`        | Crate manual `vectora-test-mocks`: dublês de teste e fixtures                           |
| `template/`     | Arquivos fixos de `rust-backend/`: README, `.gitignore`, toolchain e o binário          |
| `tools/`        | `check_targets.py` e `measure_mocks.py`                                                 |
| `reports/`      | `report.json`, `cargo-check.json` e `unresolved-patterns.log` de cada etapa             |
| `rust-backend/` | Saída: o repositório do backend Rust                                                    |

## Escopo atual

Todo o `backend/` e toda a suíte `tests/` do Vectora. Cada arquivo de `backend/` vira um módulo em `rust-backend/src/` (`pkg/__init__.py` vira `pkg/mod.rs`; a raiz vira `lib.rs`), e cada `test_*.py` vira um alvo em `rust-backend/tests/`. `backend/testing`, `conftest.py` e os fakes da suíte também são traduzidos, mas os patterns de fixtures e dublês continuam apontando para os mocks manuais de `mocks/`. Nada gerado escreve em `runtime/`, `mocks/` ou `template/`.

## Vocabulário

- **Mapa:** tabela de trocas diretas de um mesmo assunto. Cada linha diz que um elemento Python vira um elemento Rust, sem decidir nada. Exemplos: no mapa de declarações, `def` → `fn`; no de type hints, `int` → `i64`; no de builtins, `len` → `natives::len`.
- **Pattern:** regra que reconhece uma situação e escolhe quais mapas aplicar e como montar o resultado. Dois `def` diferentes, como um teste async com fixtures e um auxiliar tipado, caem em patterns diferentes.

O mapa ajuda sozinho; quem resolve uma construção inteira é o pattern. Proibido: mapa ou pattern por arquivo, por teste ou por nome de teste.

## Os mapas

Um arquivo por assunto em `maps/`: declarações, type hints, retornos, operadores, literais, builtins, biblioteca padrão, métodos, exceções, mocks e fixtures, marcas do pytest. Nome `backend.*` sem linha no mapa vira capacidade do backend, e chamá-la falha com `TDD pending`.

## Os patterns

Ficam em `maps/patterns.json`, cerca de dez:

| Pattern                 | Reconhece                                                                   |
| ----------------------- | --------------------------------------------------------------------------- |
| teste síncrono          | `def test_*` sem fixtures                                                   |
| teste async             | `async def test_*`                                                          |
| teste com fixtures      | parâmetros resolvidos pelo mapa de fixtures ou por fixture do arquivo       |
| teste parametrizado     | `pytest.mark.parametrize` com valores literais                              |
| auxiliar tipado         | função com todas as anotações no mapa de type hints                         |
| auxiliar sem tipos      | função sem anotações suficientes: tipos vêm dos usos ou ficam pendentes     |
| fixture com `yield`     | `pytest.fixture` geradora, com teardown                                     |
| bloco gerenciado        | `with`, `async with`, `pytest.raises`, `patch`                              |
| tratamento de erro      | `try`, `except`, `finally`, `raise`                                         |
| instruções e expressões | atribuição, `if`, `for`, `while`, `assert`, chamadas, operadores e coleções |

O motor só faz a mecânica: percorrer a árvore, escolher o pattern, aplicar os mapas e registrar pendências. Nenhuma equivalência fica escrita no código do motor.

## Regras de tradução

- Traduza tudo o que casa. Quando faltar regra, deixe a pendência **no menor trecho** sem regra, com `arquivo:linha` e o trecho original, e continue o restante da função.
- **O código migrado é Rust real.** Structs, enums, traits, `Option`, `Result`, coleções e funções com tipos estáticos, escolhidos pelos mapas e patterns a partir das anotações, dataclasses, modelos Pydantic e contratos. Valor dinâmico (`Object`) e reimplementação da semântica do Python são proibidos no código gerado.
- Quando o tipo ou a equivalência não puder ser determinado, registre a pendência no menor trecho; nunca recorra a um valor dinâmico para a função compilar. Dado realmente aberto, como JSON livre, usa o tipo Rust próprio (`serde_json::Value`) declarado no mapa de type hints.
- O `Object` de `runtime/` e os testes que o usam são o estado anterior a esta regra e serão substituídos. Não acrescente nada novo sobre ele.
- O runtime é código manual em `runtime/`, e mocks e fixtures em `mocks/`. Use o que já existe; se faltar algo, escreva-o lá, com teste, e registre mocks em `maps/mock-contracts.json`. A geração só copia esses crates para `rust-backend/crates/`; nunca os altera.
- Uma pendência executada é falha de migração. Chegar a uma capacidade do backend ainda ausente é o vermelho esperado do TDD. Os dois são contados separadamente.

## Como medir

Por função: assinatura traduzida, número de pendências, compila, executa e chega ao vermelho esperado. Só conta como migrado um teste sem pendências que compila e executa. O relatório ordena as formas sem regra por quantas funções ficariam completas, e a próxima linha de mapa ou pattern sai dessa lista.

Duas gerações com a mesma entrada devem produzir os mesmos arquivos.

## Comandos

```bash
cargo +stable test --manifest-path mocks/Cargo.toml --target-dir target/mocks
```

```bash
python3 tools/measure_mocks.py
```

```bash
cargo +stable run
```

```bash
python3 tools/check_targets.py --run
```

## Commits

Conventional Commits em português, um por etapa, seguido de `git push`. Toda etapa regenera `rust-backend/` com `cargo +stable run`, compila e executa com `python3 tools/check_targets.py --run` e commita `rust-backend/` com `reports/`, para comparar o andamento. Não inclua `target/`.
