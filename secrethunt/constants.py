"""Fixed values for the retired secrethunt cleanup cog."""

COG_VERSION = "1.16.0"
CONFIG_IDENTIFIER = 0x5EC8E7A1  # the retired cog's identifier: the cleanup reads its old data with it

# Only the keys the cleanup reads. Everything else the old cog stored stays untouched on disk.
DEFAULT_GUILD = {
    "pool": [],              # channel ids that held hunt objects
    "trail": [],             # forum posts that held the Treacle Well's tea tables
    "objects": {},           # puzzle id -> {channel id (str): pinned object message id}
    "poems": {},             # channel id (str) -> pinned poem message id
    "poem_targets": {},      # puzzle id -> channel id its poem went to
    "test_channel_id": None, # rehearsal channel
    "test_objects": {},      # puzzle id -> {room id (str): message id}
    "test_poems": {},        # puzzle id -> rehearsal poem message id (in the test channel)
    "finishers": {},         # puzzle id -> [user ids]
}
DEFAULT_MEMBER = {"puzzles": {}, "presses": []}
