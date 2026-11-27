# Épico: migrar os testes Python do Vectora para Rust e preparar a migração do backend

## Por que este trabalho existe

O backend do Vectora será migrado incrementalmente de Python para Rust. Seguimos TDD. Primeiro reconstruímos os contratos de teste e as dependências falsas em Rust, depois traduzimos os testes e usamos o vermelho esperado para orientar a implementação Rust real. O volume da suíte justifica uma ferramenta específica para o Vectora: cada regra deve resolver uma família de ocorrências, e o núcleo de tradução deve poder ser reutilizado no código de produção.

O motor atual é o fork local do Depyler. Reutilizamos seu parser, HIR, inferência e geração Rust. O gerador original do Vectora é referência de cobertura e fornece mapas e ports que o Depyler também deve receber. Esta etapa não depende de mudanças na Engine nem de publicação upstream do fork.

## Checkouts e artefatos

| Componente              | Local                                                                                             |
| ----------------------- | ------------------------------------------------------------------------------------------------- |
| Repositório de migração | `/mnt/externo/vectora-repos/migrate-tests/tests-rust`                                             |
| Fork local do Depyler   | `/mnt/externo/vectora-repos/migrate-tests/depyler`, branch `vectora-migration`                    |
| Backend Python          | `/mnt/externo/vectora-repos/vectora-new/backend`                                                  |
| Suíte e lockfile Python | `/mnt/externo/vectora-repos/vectora-new/tests` e `/mnt/externo/vectora-repos/vectora-new/uv.lock` |
| Engine Rust, referência | `/mnt/externo/vectora-repos/engine`                                                               |
| Mocks manuais           | `migrate-tests/tests-rust/mocks/`                                                                 |
| Python normalizado      | `migrate-tests/tests-rust/dist-python/`, saída proposta                                           |
| Rust gerado             | `migrate-tests/tests-rust/dist/`                                                                  |

Os caminhos iniciados por `migrate-tests/` são relativos a `/mnt/externo/vectora-repos`. As regras do checkout de migração estão no [AGENTS.md](../AGENTS.md). O fork recebe commits locais e permanece na branch de trabalho. Este épico não envolve PR upstream.

## Estado de referência

Na última comparação completa registrada, os dois geradores identificaram **7.204 funções em 503 arquivos**, com a mesma contagem por arquivo. O original reportou **627 funções em 53 ports manuais**. O Depyler puro emitiu corpos em **288 arquivos** e produziu **548 candidatos a teste**. Ao checar cada alvo com candidatos, **2 dos 103 alvos** compilaram, com **3 candidatos**. Fora dos ports compartilhados, **2 candidatos** estavam em um alvo compilável. Esses são números de emissão e compilação; não há evidência de execução e paridade desses candidatos no relatório. O crate do gerador original passou com 630 testes naquela rodada, uma métrica diferente da cobertura de tradução.

A comparação assistida foi uma sobreposição dos 53 ports sobre o resultado do Depyler. A integração dessa estratégia ao Cargo e à CLI padrão ainda está pendente. O Depyler já gera um manifesto Cargo por lote, preserva os caminhos relativos, registra testes ignorados quando faltam regras e isola falhas e timeouts por arquivo. O crate de mocks está protegido contra sobreposição de saída. Os mapas de pacotes e símbolos oferecem candidatos de equivalência. Conectar essas regras à resolução e à emissão é trabalho adicional.

Evidências: [comparação por arquivo](../dist/comparison-files.csv), [verificação Cargo por alvo](../dist/comparison-cargo-check.json), [inventário dos dez maiores](../maps/top-10-pattern-inventory.json), [ports existentes](../maps/manual-translations.json) e [contratos de mocks](../maps/mock-contracts.json). Revalide a revisão Python e os relatórios antes de reutilizar os números. O denominador histórico de 7.201 mudou para 7.204 na última medição.

## Resultados e escopo

1. **Mocks:** reconstruir manualmente 100% dos contratos de mocks e fixtures efetivamente usados por testes Python no escopo. O denominador deve incluir ocorrências indiretas, aliases, factories, `conftest.py` e fakes locais.
2. **Piloto:** migrar automaticamente 100% das funções em cada um dos dez arquivos abaixo. Cada caso parametrizado deve ter rastreio e execução correspondentes. Ports compartilhados dão continuidade à suíte, mas ficam fora do numerador automático.
3. **Suíte:** chegar a pelo menos 90% das funções do inventário de referência, com paridade comprovada. Com 7.204 funções, o limiar é 6.484. Recalcule quando a revisão de referência mudar.
4. **Backend:** reaproveitar a infraestrutura de tipos, símbolos, chamadas e fluxo em código de produção numa etapa posterior. Migração ampla do backend e alteração da Engine não fazem parte do aceite deste piloto.

### Os dez arquivos do piloto

| Arquivo em `vectora-new/tests/unit/` | Funções na seleção |
| ------------------------------------ | -----------------: |
| test_provider_routing_handler.py     |                118 |
| test_services_background.py          |                 73 |
| test_workspaces_view.py              |                 69 |
| test_context_graph_api.py            |                 65 |
| test_services_auth.py                |                 61 |
| test_tools_browser_devtools.py       |                 61 |
| test_engine_conversation_loop.py     |                 59 |
| test_tools_fs.py                     |                 57 |
| test_sandbox_policy.py               |                 56 |
| test_admin.py                        |                 53 |
| **Total**                            |            **672** |

A lista fica versionada pelo caminho. Mantenha a diferença de inventário explícita se a origem adicionar ou remover funções. Nomes iguais em classes diferentes e instâncias parametrizadas têm identidade própria. Os 56 testes de sandbox do port manual inicial continuam pendentes para o indicador automático.

## Arquitetura funcional

### 1. Normalizar Python para Python

Uma CLI escrita em Rust lê o Python original e escreve uma árvore Python em `dist-python/`. Deve trabalhar sobre a AST e usar edições de texto delimitadas, preservando comentários e trechos não afetados sempre que possível. Quando nenhuma regra se aplica com segurança, copie o arquivo ou o trecho sem mudança e registre a pendência. Cada regra informa identidade, condições, operação, versão e posição de origem. Reparse a saída e trate colisões entre edições. Exija determinismo e idempotência da árvore normalizada. O mapa de origem liga posições normalizadas às originais.

Exemplos para piloto: normalizar uma chamada `dict.update` com palavras-chave somente quando o receptor estiver resolvido como `dict` e a ordem de avaliação puder ser preservada; expandir casos literais de parametrização somente quando IDs, marcas e coleta puderem ser reconciliados. Use grandezas mensuráveis: alterações por regra, casos reconhecidos, testes que compilam com e sem normalização. As duas árvores Python devem poder ser comparadas com a mesma coleta pytest. Verifique o comportamento dos casos alterados em um ambiente controlado.

### 2. Transpilar Python normalizado para Rust

Use o Depyler como motor. O modo de lote lê `dist-python/` e escreve a árvore em `dist/`, mantendo exatamente a estrutura de diretórios. Para diagnóstico, suporte geração direta da fonte Python e modo puro com o mesmo conjunto de entradas. O modo assistido deve ser o padrão, com o inventário dos ports existentes, conferência de nomes e revisão da origem, resolução de dependências e proveniência por teste.

A transformação de Rust resolve símbolos qualificados e aliases, tipos e mutabilidade, argumentos posicionais e nomeados, valores opcionais, fluxo, chamadas assíncronas, erros e efeitos. Mapas JSON versionados identificam contratos de pacote, módulo, função e método. Cada regra executável precisa de assinatura reconhecida, condições e conversão explícita de argumentos, retorno e erro. Equivalências apenas sugeridas ficam identificadas como candidatas. O núcleo genérico de conversão é compartilhável com a migração posterior do backend.

O gerador produz um único `Cargo.toml`, os testes Rust espelhados, `README.md`, mapa de padrões, mapa de dependências e `unresolved-patterns.log`. Nunca escreva no crate de mocks ou na fonte Python. Para uma entrada que falha, preserve seu caso como ignorado e registre o motivo original. O relatório precisa distinguir falha de ferramenta, caso sem regra, saída inválida, Cargo vermelho por erro de compilação, falha esperada de TDD e divergência semântica.

### 3. Mocks e fixtures manuais

O código de mocks continua manual e versionado. Só o teste consumidor é gerado. Reconstrua os fakes por contrato, reaproveitando o mesmo tipo Rust em todos os consumidores equivalentes. Estude implementações e fixtures da Engine para decidir quando usar traits tipadas, um servidor em loopback, Tokio, armazenamento em memória ou recursos reais isolados. Defina efeitos e observabilidade requeridos antes de escolher bibliotecas.

Inventarie chamada direta e indireta, protocolo de mock, assinatura, fixture associada e casos consumidores. Priorize ambientes e dependências compartilhadas, como factory e readiness de provider, cliente HTTP, lifecycle de subprocessos, storage, streams e browser. A fixture `client` do maior arquivo já apareceu em 52 funções. Um fake por contrato reduz a duplicação de regras e de setup. [Inventário inicial e decisões de mocks](mocks-rust.md)

### 4. TDD e proveniência

Se a capacidade Rust ainda não existe, um teste traduzido pode compilar e falhar pelo motivo funcional esperado. Esse estado é útil para orientar a implementação, mas deve aparecer separado do sucesso. Erro de compilação, mock que devolve a resposta da asserção sem exercitar o contrato e teste ignorado ficam fora desse estágio. A condição verde exige preservar o comportamento e verificar o backend Rust real no ponto que o Python exercitava. Mocks permanecem nos limites em que a suíte Python isolava dependências.

## Entregas e dependências

- [ ] **A. Inventário e proteção dos mocks.** Preservar a fronteira de escrita já implementada. Enumerar contratos, aliases, factories, fixtures e consumidores. Registrar os mocks portados e os gaps com fonte e linha.
- [ ] **B. Mocks do piloto.** Implementar os fakes manuais por família, com argumentos, ordem, resultados, erros, chamadas, awaits, estado e teardown conforme o original. Conectar cada fake ao ponto de injeção do teste. B pode avançar em paralelo à normalização.
- [ ] **C. Normalizador Rust v1.** CLI, árvore Python separada, origem, diagnósticos e regras iniciais. Demonstrar preservação de comportamento nos casos transformados e medir diferença de conversão em relação à entrada crua.
- [ ] **D. Depyler assistido por padrão.** Consumir os ports do mesmo inventário, reutilizar dependências Cargo, separar proveniência e preservar o modo puro para comparação. Estender mapas por assinatura e acrescentar regras executáveis conforme o próximo bloqueio observado.
- [ ] **E. Primeiro lote reproduzível de testes.** Atacar o grupo com maior ganho marginal real entre routing de providers e background. Confirmar que ao menos uma regra do lote se reaplica em outro arquivo. Avaliar separadamente o ganho em descoberta, compilação e execução.
- [ ] **F. Fechar o piloto.** Reordenar o trabalho pelo número de testes que perdem a última pendência. Resolver primeiro famílias compartilhadas de fixtures e chamadas. Fazer o ciclo de gerar, comparar, validar e commitar por lote.
- [ ] **G. Escalar e auditar.** Ampliar o inventário até 100% dos contratos de mocks e alcançar o limiar de tradução da suíte. Avaliar o motor do Depyler pelo ganho automático verificado sob o mesmo apoio de ports. Se o gerador original se mostrar mais produtivo, preservar a comparação e reaproveitar aprendizados e regras do Depyler no caminho escolhido.

## Critérios de aceite

- [ ] A coleta de referência e a tradução reconhecem a mesma identidade e quantidade de testes e parâmetros no recorte versionado. Conversões interrompidas e casos sem equivalência aparecem na saída como pendências rastreáveis.
- [ ] O gerador normaliza somente transformações verificáveis. A saída mantém o comportamento original dos casos afetados e pode ser gerada duas vezes com resultado idêntico. A fonte Python não sofre alterações.
- [ ] Os mocks são implementados manualmente em `mocks/`. A geração não toca a árvore manual, inclusive quando a configuração ou um link simbólico aponta a saída para ela. Os testes de consumo preservam os observáveis do contrato.
- [ ] Os ports compartilhados são fornecidos ao original e ao Depyler assistido sob o mesmo critério. O relatório separa resultados manuais, automáticos e pendentes. Nenhuma linha conta como tradução automática só porque foi copiada de um port.
- [ ] O lote Rust usa um manifesto Cargo e passa pelo fluxo normal de compilação e execução. Estados de compile check, execução, vermelho funcional e paridade ficam separados. Casos ignorados, testes sem asserção equivalente e corpos que só fazem o teste passar ficam excluídos.
- [ ] Os dez arquivos atingem 100% de tradução automática com casos preservados. A suíte ampla atinge o limiar de 90% em uma revisão registrada. Os 100% de mocks são auditados no inventário de contratos e nos consumidores.
- [ ] README, logs e relatórios são regenerados no mesmo lote. Registre comandos, revisão de origem, mapa de versões das regras e resultados por arquivo. Faça commits locais por etapa e preserve os artefatos de comparação.

## Como decidir o próximo investimento

Priorize o número de testes que perdem a última pendência por hora de engenharia, incluindo revisão da saída. Diferencie frequência de uma construção e cobertura efetiva. Uma regra de `assert` pode aparecer centenas de vezes sem completar nenhum teste enquanto faltarem os mocks, o setup e o contrato da API. Compare execuções sobre a mesma revisão Python e use o mesmo catálogo manual nos dois motores. Se o ganho até compilação e paridade continuar baixo, rastreie se a causa está na normalização, na resolução de símbolos, na HIR do Depyler ou nas APIs Rust ainda ausentes. Essa evidência orienta a decisão entre ampliar o fork e reaproveitar partes dele no gerador original.
