extends Node

const MAIN_SCENE: PackedScene = preload("res://scenes/main.tscn")

var failures: Array[String] = []


func _ready() -> void:
	call_deferred("_run")


func _run() -> void:
	var game: Node2D = MAIN_SCENE.instantiate()
	add_child(game)
	for _frame in 12:
		await get_tree().physics_frame

	var player: CharacterBody2D = game.get("player")
	var enemies: Array = game.get("enemies")
	_check(player != null, "cena principal instancia o jogador")
	_check(enemies.size() == 1, "cena principal instancia o inimigo")

	player.apply_movement(1.0)
	_check(player.velocity.x > 0.0, "jogador se move para a direita")
	player.apply_movement(-1.0)
	_check(player.velocity.x < 0.0, "jogador se move para a esquerda")
	player.apply_movement(0.0, true)
	_check(player.velocity.y < 0.0, "jogador salta quando está no chão")

	var enemy: CharacterBody2D = enemies[0]
	var starting_health: int = player.get("health")
	enemy.attack_player(player)
	_check(player.get("health") == starting_health - 1, "contato causa dano ao jogador")

	var bullet: Area2D = player.shoot(Vector2.RIGHT)
	_check(bullet.get_parent() == self, "jogador instancia o projétil na cena")
	bullet.hit(enemy)
	var finishing_bullet: Area2D = player.shoot(Vector2.RIGHT)
	finishing_bullet.hit(enemy)
	_check(GameManager.status == "won", "derrotar o último inimigo encerra em vitória")

	game.restart_game()
	_check(GameManager.status == "playing", "reinício retorna ao estado jogando")
	_check(GameManager.health == 3, "reinício recupera a vida")
	_check(game.get("enemies").size() == 1, "reinício recria o inimigo")
	var restarted_enemy: CharacterBody2D = game.get("enemies")[0]
	var restarted_player: CharacterBody2D = game.get("player")
	restarted_enemy.attack_player(restarted_player)
	restarted_enemy.set("attack_cooldown", 0.0)
	restarted_enemy.attack_player(restarted_player)
	restarted_enemy.set("attack_cooldown", 0.0)
	restarted_enemy.attack_player(restarted_player)
	_check(GameManager.status == "lost", "vida esgotada encerra em derrota")

	game.queue_free()
	await get_tree().process_frame
	if failures.is_empty():
		print("GODOT_GAMEPLAY_REGRESSION_PASSED")
		get_tree().quit(0)
	else:
		for failure in failures:
			push_error(failure)
		get_tree().quit(1)


func _check(condition: bool, description: String) -> void:
	if condition:
		print("PASS: ", description)
	else:
		failures.append(description)
