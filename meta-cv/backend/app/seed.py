"""Load a small starter graph so the factual tier has something to verify against.

    python -m app.seed
"""
from .db import init_db
from .graph_store import get_store
from .ingest import record_manual_rule

RELATIONS = [  # name, functional, transitive
    ("born_in", True, False),
    ("published_in", True, False),
    ("located_in", False, True),
]

FACTS = """
pride and prejudice | written_by | jane austen
pride and prejudice | published_in | 1813
elizabeth bennet | character_in | pride and prejudice
fitzwilliam darcy | character_in | pride and prejudice
jane bennet | character_in | pride and prejudice
charles bingley | character_in | pride and prejudice
elizabeth bennet | marries | fitzwilliam darcy
jane bennet | marries | charles bingley
harry potter and the philosopher's stone | written_by | j. k. rowling
harry potter and the philosopher's stone | published_in | 1997
harry potter | character_in | harry potter and the philosopher's stone
hermione granger | character_in | harry potter and the philosopher's stone
ron weasley | character_in | harry potter and the philosopher's stone
lord voldemort | character_in | harry potter and the philosopher's stone
to kill a mockingbird | written_by | harper lee
to kill a mockingbird | published_in | 1960
atticus finch | character_in | to kill a mockingbird
scout finch | character_in | to kill a mockingbird
tom robinson | character_in | to kill a mockingbird
atticus finch | defends | tom robinson
1984 | written_by | george orwell
1984 | published_in | 1949
winston smith | character_in | 1984
jane austen | born_in | steventon
j. k. rowling | born_in | yate
harper lee | born_in | monroeville
george orwell | born_in | motihari
marie curie | born_in | warsaw
marie curie | won | nobel prize in physics
marie curie | won | nobel prize in chemistry
marie curie | discovered | polonium
marie curie | discovered | radium
albert einstein | born_in | ulm
albert einstein | won | nobel prize in physics
albert einstein | developed | theory of relativity
isaac newton | formulated | laws of motion
isaac newton | formulated | law of universal gravitation
warsaw | located_in | poland
ulm | located_in | germany
poland | located_in | europe
germany | located_in | europe
"""


def main() -> None:
    init_db()
    store = get_store()
    for name, functional, transitive in RELATIONS:
        store.define_relation(name, functional, transitive)
        record_manual_rule(name, functional, transitive)
    n = 0
    for line in FACTS.strip().splitlines():
        s, r, o = (p.strip() for p in line.split("|"))
        store.add_fact(s, r, o, "seed")
        n += 1
    print(f"seeded {n} facts and {len(RELATIONS)} relation rules")


if __name__ == "__main__":
    main()
