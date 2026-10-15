"""Offline terminal lettering: exact case-sensitive spelling, independent of photo resolution."""

LETTERS = {
    "V": ("#   #", "#   #", "#   #", "#   #", " # # ", " # # ", "  #  "),
    "a": ("     ", "     ", " ### ", "    #", " ####", "#   #", " ####"),
    "s": ("     ", "     ", " ####", "#    ", " ### ", "    #", "#### "),
    "t": (" #  ", " #  ", "### ", " #  ", " #  ", " #  ", "  ##"),
    "r": ("    ", "    ", "# ##", "##  ", "#   ", "#   ", "#   "),
}
LOGO = tuple(" ".join(LETTERS[letter][row] for letter in "Vastar") for row in range(7))
PALETTE = ((101, 186, 218), (144, 153, 211), (174, 139, 202), (206, 151, 171),
           (220, 183, 158))


def logo_color(column):
    position = column / (len(LOGO[0]) - 1) * (len(PALETTE) - 1)
    index = min(int(position), len(PALETTE) - 2)
    weight = position - index
    return tuple(round(a * (1 - weight) + b * weight)
                 for a, b in zip(PALETTE[index], PALETTE[index + 1], strict=True))
