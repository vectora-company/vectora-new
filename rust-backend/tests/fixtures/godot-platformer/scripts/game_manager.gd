extends Node

signal state_changed

var health: int = 3
var max_health: int = 3
var enemies_remaining: int = 0
var status: String = "ready"


func _ready() -> void:
	_register_key("move_left", KEY_A)
	_register_key("move_left", KEY_LEFT)
	_register_key("move_right", KEY_D)
	_register_key("move_right", KEY_RIGHT)
	_register_key("jump", KEY_SPACE)
	_register_key("shoot", KEY_J)
	_register_key("restart", KEY_R)


func _register_key(action: String, key: Key) -> void:
	if not InputMap.has_action(action):
		InputMap.add_action(action)
	var event := InputEventKey.new()
	event.physical_keycode = key
	InputMap.action_add_event(action, event)


func start_game(enemy_count: int) -> void:
	max_health = 3
	health = max_health
	enemies_remaining = enemy_count
	status = "playing"
	state_changed.emit()


func player_damaged(amount: int = 1) -> void:
	if status != "playing":
		return
	health = maxi(health - amount, 0)
	if health == 0:
		status = "lost"
	state_changed.emit()


func enemy_defeated() -> void:
	if status != "playing":
		return
	enemies_remaining = maxi(enemies_remaining - 1, 0)
	if enemies_remaining == 0:
		status = "won"
	state_changed.emit()
