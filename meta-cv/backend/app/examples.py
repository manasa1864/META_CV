"""First-screen example questions: one per tier and one per kind of caught hallucination.
Shared by the UI chips (GET /examples), the replay recorder and the preflight check."""

EXAMPLES = [
    {"id": "false-premise", "label": "False premise", "tier": "factual",
     "turns": ["Why did Albert Einstein win the Nobel Prize in 1905?"]},
    {"id": "fact-check", "label": "Facts checked against the graph", "tier": "factual",
     "turns": ["Who wrote Pride and Prejudice, and where was the author born?"]},
    {"id": "calculation", "label": "Checked calculation", "tier": "reasoning",
     "turns": ["A shop has 12 boxes with 7 apples each. 4 apples are eaten. How many apples are left?"]},
    {"id": "mixed", "label": "Facts, then a calculation", "tier": "reasoning",
     "turns": ["How many years after Pride and Prejudice was 1984 published?"]},
    {"id": "follow-up", "label": "Follow-up question", "tier": "consistency",
     "turns": ["Who wrote 1984?", "Where was he born?"]},
]
