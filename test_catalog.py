from catalog import bulk_update, related_game_ids


def main():
    games = [
        {"game_id": "game-a", "name": "Alpha", "platform": "NES", "genre": "Action Adventure", "series": "Saga"},
        {"game_id": "game-b", "name": "Beta", "platform": "NES", "genre": "Action", "series": "Saga"},
        {"game_id": "game-c", "name": "Gamma", "platform": "PC", "genre": "Strategy"},
        {"game_id": "game-d", "name": "Hidden", "platform": "NES", "genre": "Action", "series": "Saga", "hidden": True},
    ]
    assert related_game_ids(games, 0) == [1]
    # Mixed stable ids and integer indexes must not raise TypeError.
    assert bulk_update(games, ["game-b", 1], {"progress": "Completed", "rating": 4.5, "favorite": True}) == 1
    assert games[1]["rating"] == 4.5 and games[1]["progress"] == "Completed"
    try:
        bulk_update(games, [0], {"rating": 6})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid rating accepted")
    print("catalog self-test: ok")


if __name__ == "__main__":
    main()
