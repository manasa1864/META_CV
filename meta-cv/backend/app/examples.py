"""First-screen example questions: realistic, multi-part questions that exercise each tier, a caught
hallucination, and compression. Shared by the UI chips (GET /examples), the replay recorder and preflight.
`separate`: ask each turn in a fresh conversation (two similar questions, not a follow-up)."""

EXAMPLES = [
    {"id": "false-premise", "label": "False premise", "tier": "factual",
     "turns": ["Why did Albert Einstein win the Nobel Prize in 1905?"]},
    {"id": "multi-fact", "label": "Several facts at once", "tier": "factual",
     "turns": ["Who wrote Pride and Prejudice, when was it published, and where was the author born?"]},
    {"id": "comparison", "label": "Compare two people", "tier": "factual",
     "turns": ["Were Marie Curie and Albert Einstein born in the same country?"]},
    {"id": "mixed", "label": "Facts, then a calculation", "tier": "reasoning",
     "turns": ["How old was Harper Lee when To Kill a Mockingbird was published?"]},
    {"id": "reuse", "label": "Reuse a verified calculation", "tier": "reasoning", "separate": True,
     "turns": ["A shop sells 3 shirts at $24.99 each with 8% sales tax. What is the total?",
               "A shop sells 5 hats at $12.50 each with 8% sales tax. What is the total?"]},
    {"id": "follow-up", "label": "Follow-up question", "tier": "consistency",
     "turns": ["Who wrote 1984?", "How many years after his birth was it published?"]},
]
