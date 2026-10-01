"""Static config for IntroForm: the form's questions, colors, and Config defaults."""

# Embed accent color (Wonderland pink).
BRAND_COLOR = 0xFF8FB1

# custom_ids for the persistent panel buttons. Must stay stable across
# restarts/updates or already-posted panels stop responding.
BTN_OPEN_ID = "introform:open"
BTN_DELETE_ID = "introform:delete"
BTN_FIND_ID = "introform:find"

# Discord modals allow at most 5 inputs, so a few topics share a field.
# field_name=None means the answer is used as the embed title instead.
QUESTIONS = [
    {
        "key": "name",
        "label": "Name / nickname",
        "style": "short",
        "required": True,
        "max_length": 40,
        "placeholder": "What should people call you?",
        "field_name": None,
        "inline": False,
    },
    {
        "key": "age_gender",
        "label": "Age & gender",
        "style": "short",
        "required": True,
        "max_length": 40,
        "placeholder": "e.g. 28, she/her",
        "field_name": "Age & Gender",
        "inline": True,
    },
    {
        "key": "location",
        "label": "Location",
        "style": "short",
        "required": False,
        "max_length": 60,
        "placeholder": "City / country / timezone",
        "field_name": "Location",
        "inline": True,
    },
    {
        "key": "games",
        "label": "Games I play",
        "style": "paragraph",
        "required": True,
        "max_length": 300,
        "placeholder": "What are you playing these days?",
        "field_name": "Games I Play",
        "inline": False,
    },
    {
        "key": "extra",
        "label": "Anything else you'd like to share",
        "style": "paragraph",
        "required": False,
        "max_length": 500,
        "placeholder": "Hobbies, fun facts, what you're looking for here...",
        "field_name": "About Me",
        "inline": False,
    },
]

GUILD_DEFAULTS = {
    "channel_id": None,
    "panel_message_id": None,
    # Delete free-form messages from non-mods in the intros channel.
    "enforce": True,
    # str(user_id) -> {"message_id": int, "answers": {key: str}}
    "intros": {},
}

# Max results shown in the keyword-search dropdown (Discord caps a select at 25 options).
SEARCH_LIMIT = 25

# Seconds between "use the form" notices to the same user.
NOTICE_COOLDOWN = 30.0
NOTICE_DELETE_AFTER = 10
