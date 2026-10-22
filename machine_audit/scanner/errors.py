"""Scanner invariants carry source-owned codes, never filesystem messages."""


class ScanError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
