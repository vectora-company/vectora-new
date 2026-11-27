extends Node2D

const PLAYER_SCENE: PackedScene = preload("res://scenes/player.tscn")
const ENEMY_SCENE: PackedScene = preload("res://scenes/enemy.tscn")

@onready var actors: Node2D = $Actors
@onready var status_label: Label = $HUD/Status

var player: CharacterBody2D
var enemies: Array[CharacterBody2D] = []


func _ready() -> void:
	GameManager.state_changed.connect(_update_status)
	restart_game()


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("restart"):
		restart_game()
		return
	if event.is_action_pressed("shoot") and GameManager.status == "playing":
		var direction: Vector2 = Vector2.RIGHT
		if not enemies.is_empty() and enemies[0].global_position.x < player.global_position.x:
			direction = Vector2.LEFT
		player.shoot(direction)
		return
	if GameManager.status != "playing" or not is_instance_valid(player):
		return


func _physics_process(_delta: float) -> void:
	if GameManager.status != "playing" or not is_instance_valid(player):
		return
	var horizontal: float = Input.get_axis("move_left", "move_right")
	player.apply_movement(horizontal, Input.is_action_just_pressed("jump"))


func restart_game() -> void:
	for actor in actors.get_children():
		actors.remove_child(actor)
		actor.queue_free()
	enemies.clear()
	GameManager.start_game(1)
	player = PLAYER_SCENE.instantiate()
	player.position = Vector2(120.0, 380.0)
	actors.add_child(player)
	var enemy: CharacterBody2D = ENEMY_SCENE.instantiate()
	enemy.position = Vector2(520.0, 380.0)
	enemy.set("target", player)
	enemies.append(enemy)
	actors.add_child(enemy)
	_update_status()


func _update_status() -> void:
	if not is_instance_valid(status_label):
		return
	status_label.text = "Vida: %d   Inimigos: %d   Estado: %s" % [
		GameManager.health,
		GameManager.enemies_remaining,
		GameManager.status,
	]
