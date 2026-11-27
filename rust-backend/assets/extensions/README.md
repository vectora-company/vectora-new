# Host de extensões Vext no Vectora

Este documento descreve o contrato que o Vectora implementa hoje. Ele não
define o SDK, o manifesto nem o formato de pacote do Vext: esses contratos
pertencem ao repositório `vectora-company/vext` e precisam ser compatibilizados
antes de integrar um catálogo ou instalador remoto.

## Limite entre os produtos

- O **Vext** é dono do SDK, manifesto, publicação e distribuição de extensões.
- O **Vectora** é dono da autorização local, estado instalado, ciclo de vida,
  broker de tools, isolamento de interface, auditoria e contribuição aos
  workbenches.
- O host recebe um `InstallRequest` já resolvido com metadados e um pacote
  extraído dentro de seu diretório de bundles. Ele recalcula o SHA-256 do
  conteúdo antes de ativar. A origem do pacote e a validação de assinatura não
  são responsabilidade nem capacidade deste endpoint.
- A API do host usa os modelos em `backend/extensions/models.py`; não se deve
  inferir que esses modelos sejam o manifesto ou o SDK público do Vext.

## Consumidores

1. A API FastAPI de `backend/api/handlers/extensions.py` expõe operações locais
   autenticadas para administração e leitura.
2. `backend/tools/extensions.py` registra `call_extension_tool` no harness.
   O broker repassa o nome e os argumentos explícitos da tool ao processo MCP;
   não encaminha transcript, memória, contexto do modelo nem credenciais.
3. `frontend/lib/extensions/registry.ts` valida, ordena e filtra contribuições
   tipadas. Os workbenches, Settings, header e File System Workbench consomem
   essas contribuições pelos adapters do app.
4. `frontend/components/extensions/extension-frame.tsx` renderiza páginas
   aprovadas em iframe isolado. Mensagens da extensão são reduzidas a `ready` e
   `resize`; a interface não recebe acesso aos stores do Vectora.

## API observável

Todas as rotas exigem usuário autenticado. As operações marcadas como
**admin** também exigem role `admin` ou `root`.

| Método e rota                        | Acesso      | Comportamento                                                       |
| ------------------------------------ | ----------- | ------------------------------------------------------------------- |
| `GET /extensions`                    | autenticado | Lista resumos instalados, sem caminho local, comando ou segredo.    |
| `GET /extensions/contributions`      | autenticado | Lista contribuições de extensões ativas.                            |
| `GET /extensions/{id}`               | autenticado | Retorna um resumo da extensão.                                      |
| `GET /extensions/{id}/health`        | autenticado | Informa estado, processo conectado e tools descobertas.             |
| `GET /extensions/{id}/ui/{path}`     | autenticado | Serve arquivos permitidos de `ui/`, com CSP sem rede e `nosniff`.   |
| `GET /extensions/{id}/audit?limit=N` | admin       | Lê até 500 eventos de auditoria dessa extensão.                     |
| `POST /extensions`                   | admin       | Instala o `InstallRequest` já resolvido e guarda a versão anterior. |
| `POST /extensions/{id}/activate`     | admin       | Valida novamente o bundle e inicia o servidor MCP.                  |
| `POST /extensions/{id}/deactivate`   | admin       | Encerra o cliente e desativa a extensão.                            |
| `POST /extensions/{id}/rollback`     | admin       | Torna a versão anterior atual e a deixa inativa.                    |
| `POST /extensions/{id}/revoke`       | admin       | Encerra e marca a extensão como revogada.                           |
| `PUT /extensions/{id}/grants`        | admin       | Substitui tools, hosts de rede e workspaces concedidos.             |
| `DELETE /extensions/{id}`            | admin       | Encerra, remove bundles contidos no host e apaga o registro.        |

Não existe endpoint separado de update: `POST /extensions` instala a nova
versão para o mesmo ID e preserva a versão atual como `previous`. Também não há
endpoint HTTP para chamar tools; o despacho é interno ao harness. A health
route informa estado local, não é uma sonda de saúde publicada pelo Vext.

Erros de domínio têm códigos estáveis (`not_found`, `denied`, `revoked`,
`conflict`, `not_active`, `invalid`, `incompatible`, `integrity` e `failed`),
convertidos pela API em status HTTP 4xx/502. Respostas de resumo não expõem o
caminho instalado, comando de inicialização nem ambiente do processo.

## Ciclo de vida e persistência

O registro local fica em `<vectora_home>/extensions/registry.json`; a auditoria
append-only fica em `audit.jsonl`. A gravação do registro é atômica. No startup,
o servidor tenta reativar extensões que estavam ativas; no shutdown encerra os
clientes supervisionados.

Estados observáveis: `inactive`, `active`, `failed` e `revoked`. Instalação,
concessão, ativação, desativação, rollback, revogação, remoção e chamadas de
tool registram o ator e uma descrição limitada na auditoria. Alterar hosts
concedidos reinicia a extensão para aplicar a nova política de rede.

## Segurança e isolamento

- A ativação recalcula o digest, valida versão da API, comando, caminho dentro
  de `bundles/` e IDs duplicados de contribuição. O pacote não pode exceder
  5.000 arquivos ou 200 MiB, nem conter links simbólicos.
- O processo é iniciado pelo cliente MCP com sandbox obrigatório, ambiente
  vazio e somente hosts de rede concedidos. Startup e chamadas têm timeout.
- O broker exige extensão ativa, tool declarada e concedida e workspace
  incluído no grant. Argumentos são JSON serializáveis e limitados a 256 KiB.
- A UI aceita somente arquivos sob `ui/`, limita cada arquivo a 5 MiB e define
  CSP sem conexões de rede. O iframe usa `sandbox="allow-scripts"`, sem
  `allow-same-origin`; a ponte só aceita mensagens do próprio frame e apenas
  para sinalizar prontidão ou altura.
- Ausência de grant, incompatibilidade, digest inválido, colisão de ID ou
  extensão inativa resulta em recusa; uma falha de tool encerra o cliente e
  marca a extensão como `failed`.

## Limites e próximos contratos

O host local não baixa artefatos, não valida assinatura do publicador, não
implementa catálogo Vext e não promete compatibilidade com um manifesto externo.
Antes de ligar um catálogo, o Vext e o Vectora precisam alinhar: fonte de
artefatos verificados, assinatura e identidade do publicador, versão do
manifesto, política de atualização e quem fornece o `InstallRequest`. Não
adicione esses campos ao contrato público do Vectora por inferência.

Os grants são persistidos por extensão, com lista de workspaces. O runtime
precisa continuar conferindo o workspace em cada chamada; instalar ou ativar
uma extensão não concede acesso automaticamente a novos workspaces.
