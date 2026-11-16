"""LinkedIn Data Archive adapter.

    adapter.py     parses Connections.csv, Invitations.csv, messages.csv
    vocabulary.py  what LinkedIn calls a tie ("1st", "invitation", "connection")

Importing this package registers both with the pipeline.
"""

from .adapter import LinkedInAdapter
from .vocabulary import VOCABULARY

__all__ = ["VOCABULARY", "LinkedInAdapter"]
