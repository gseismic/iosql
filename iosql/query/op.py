from enum import IntEnum


class Op(IntEnum):
    BetweenAnd  = 0
    GT          = 1
    GE          = 2
    LT          = 3
    LE          = 5
    In          = 6
