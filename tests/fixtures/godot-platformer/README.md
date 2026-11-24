# Fixture de regressão Godot 4.6

Projeto descartável de plataforma usado para validar a integração Godot sem
abrir ou alterar arquivos do workspace do usuário. Contém cenas separadas para
jogador, inimigo e projétil; movimento lateral, salto, disparo, dano por contato,
vitória, derrota e reinício.

Para abrir e jogar com Godot 4.6:

```text
godot --path tests/fixtures/godot-platformer
```

Controles: `A`/`D` ou setas movem, `Espaço` salta, `J` dispara e `R` reinicia.

Para executar a regressão de gameplay sem interface:

```text
VECTORA_GODOT_EXECUTABLE=/caminho/para/godot uv run --no-sync pytest tests/integration/test_godot_platformer_fixture.py -q
```

O teste cria uma cópia temporária do projeto, troca a cena inicial pelo runner,
usa um prazo máximo e encerra a árvore de processos se o engine travar. A cópia
descartável inclui o import/cache do Godot; nada é gravado na fixture original.
