"""People in documents and emails: find them, give each one a stable label ("Person 7"),
and replace their names and email addresses everywhere.

Names are learned from:
  1. email headers (From, To, Cc display names and addresses),
  2. spreadsheet name columns (passed in as tokens),
  3. text: a common first name followed by a capitalized surname ("Priya Raman"), titles
     ("Mr. Hale"), greetings ("Hi Marisol,") and sign-offs ("Thanks,\nDarnell Okafor").

Labels keep conversations readable for AI buyers (who wrote to whom) without saying who
anyone is. The label key (label -> real name and emails) goes ONLY to the private folder.
Company names are not people and are kept.
"""
from __future__ import annotations

import csv
import os
import re

# Common US first names (both sexes). Used only to spot "Firstname Lastname" in text; a
# name not on this list is still caught when it appears in an email header, a greeting,
# a signature or a spreadsheet name column.
FIRST_NAMES = set("""
aaron abby abigail adam adrian aiden alan albert alex alexa alexander alexis alice alicia
allison alyssa amanda amber amy ana andre andrea andrew angela angelica angie anita ann anna
anne annie anthony antonio april arthur ashley audrey austin barbara becky ben benjamin beth
betty beverly bill billy blake bob bobby bonnie brad bradley brandon brenda brent brett brian
brittany brooke bruce bryan caleb cameron carl carla carlos carmen carol caroline carolyn
carrie casey catherine cathy chad charles charlie charlotte chelsea cheryl chris christian
christina christine christopher cindy claire clara clarence claudia clifford cody colin
connie connor courtney craig crystal curtis cynthia dale dan dana daniel danielle danny darnell
darlene darren dave david dawn debbie deborah debra denise dennis derek devin diana diane
dolores don donald donna doris dorothy doug douglas dustin dylan earl ed eddie edgar edith
edward eileen elaine eleanor elena eli elijah elizabeth ella ellen emily emma eric erica erik
erin ernest esther ethan eugene eva evan evelyn felicia fernando frances francisco frank fred
frederick gabriel gail gary george gerald gina glenn gloria gordon grace greg gregory hailey
hannah harold harry hazel heather hector helen henry holly howard ian irene isaac isabel
isabella jack jackie jacob jacqueline jake james jamie jan jane janet janice jared jasmine
jason javier jay jean jeff jeffrey jenna jennifer jenny jeremy jerry jesse jessica jesus jill
jim jimmy jo joan joanne joe joel john johnny jon jonathan jordan jorge jose joseph josh joshua
joy joyce juan judith judy julia julian julie justin karen kate katherine kathleen kathryn
kathy katie kayla keith kelly ken kenneth kevin kim kimberly kristen kristin kyle larry laura
lauren lawrence leah lee leon leonard leslie linda lindsay lisa logan lois lori lorraine louis
lucas luis luke lynn madison maggie mandy manuel marcus margaret maria marie marilyn mario
marisol marjorie marlene martha martin marvin mary matt matthew megan melanie melissa melvin
meredith michael michele michelle miguel mike mildred miranda molly monica morgan nancy natalie
nathan nathaniel neil nicholas nick nicole noah norma olivia oscar pamela pat patricia patrick
paul paula pedro peggy penny peter phil philip phillip phyllis priya rachel ralph ramon randy
raul ray raymond rebecca regina renee rhonda ricardo richard rick ricky rita rob robert roberto
robin rodney roger ron ronald rosa ruby russell ruth ryan sabrina sally samantha samuel sandra
sara sarah scott sean sergio seth shane shannon sharon shawn sheila shelly sherry shirley
sophia stacy stanley stephanie stephen steve steven sue susan suzanne sydney tammy tanya tara
taylor teresa terri terry theresa thomas tiffany tim timothy tina tobias todd tom tommy tony
tracy travis trevor troy tyler valerie vanessa veronica vicki victor victoria vincent virginia
walter wanda wayne wendy william willie yolanda yvonne zachary
""".split())
# first names that are also everyday words: only counted with a surname or in a greeting
_AMBIGUOUS = {"will", "bill", "mark", "grace", "joy", "hope", "rose", "dawn", "pat", "rob",
              "sue", "ray", "jay", "dale", "glenn", "jean", "may", "april", "ruby", "lee",
              "don", "art", "chase", "dean", "faith", "frank", "gail", "jack", "jan", "rich",
              "rick", "sally", "sharon", "taylor", "victor", "jo", "ed", "dan", "tim", "lynn",
              "morgan", "jordan", "logan", "madison", "austin", "carmen", "angel", "eli"}
_NOT_SURNAME = {"the", "and", "for", "from", "with", "this", "that", "please", "thanks",
                "thank", "regards", "street", "st", "ave", "road", "llc", "inc", "co",
                "company", "construction", "corp", "group", "department", "dept", "team",
                "office", "project", "job", "change", "order", "monday", "tuesday",
                "wednesday", "thursday", "friday", "saturday", "sunday", "january",
                "february", "march", "june", "july", "august", "september", "october",
                "november", "december", "sent", "subject", "from", "to", "cc", "bcc",
                "date", "attachments", "attachment", "importance", "re", "fw", "fwd",
                "mailto", "cell", "mobile", "phone", "fax", "email", "tel", "direct",
                "main", "www", "http", "https", "caution", "external", "warning"}

_CAP = r"[A-Z][a-z'\-]+(?:-[A-Z][a-z'\-]+)?"
_SP = r"[ \t]+"  # names never run across a line break ("Mark\nSent:" is not a person)
# lookahead so overlapping pairs are all seen: "Inspector Priya Raman" -> "Priya Raman"
RE_FULL = re.compile(rf"(?=\b({_CAP})(?:{_SP}[A-Z]\.)?{_SP}({_CAP})\b)")
RE_GREET = re.compile(rf"(?im)^[ \t]*(?:hi|hello|hey|dear|good morning|good afternoon){_SP}"
                      rf"({_CAP})(?:{_SP}({_CAP}))?[ \t]*[,:!]")
RE_SIGNOFF = re.compile(r"(?im)^[ \t]*(?:thanks|thank you|regards|best regards|best|sincerely|"
                        r"respectfully|cheers|kind regards|warm regards)[,.!]?[ \t]*$\n+[ \t]*"
                        rf"({_CAP})(?:{_SP}[A-Z]\.)?(?:{_SP}({_CAP}))?[ \t]*$")
RE_TITLED = re.compile(rf"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof)\.?{_SP}({_CAP})(?:{_SP}({_CAP}))?")
RE_ADDR = re.compile(r"([^<>,;\"]*?)\s*<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?")
# a name written next to an email address anywhere in a text, e.g. forwarded headers:
# "From: Pat Doe <pat@x.com>", "Pat Doe [mailto:pat@x.com]", "Lee, Pat (pat@x.com)"
RE_INLINE_ADDR = re.compile(
    r"([A-Z][A-Za-z'\-\.]+(?:[ \t,]+[A-Z][A-Za-z'\-\.]+){0,3})[ \t]*[<\[(][ \t]*(?:mailto:)?"
    r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})[ \t]*[>\])]")
RE_HEADER_LINE = re.compile(rf"(?im)^[ \t]*(?:from|to|cc|bcc|attn|attention|contact)[ \t]*:"
                            rf"[ \t]*({_CAP}(?:{_SP}{_CAP}){{1,2}})[ \t]*$")


# display names next to an address that are really mailboxes, not people
COMMON_NAMES_BLOCK = {"info", "admin", "support", "billing", "accounts", "sales", "office",
                      "noreply", "no reply", "do not reply", "customer service", "help desk",
                      "accounts payable", "accounts receivable", "reception", "orders"}


def _looks_like_person(name: str, email: str | None) -> bool:
    parts = [p for p in re.findall(r"[A-Za-z][A-Za-z'\-]+", name)]
    if not 1 <= len(parts) <= 4:
        return False
    if parts[0].lower() in FIRST_NAMES:
        return True
    local = (email or "").split("@")[0].lower()
    return bool(local) and any(len(p) > 2 and p.lower() in local for p in parts)


class PeopleBook:
    def __init__(self):
        self.labels: dict[str, str] = {}      # person key -> "Person N"
        self.names: dict[str, set] = {}       # person key -> full names seen
        self.emails: dict[str, set] = {}      # person key -> emails
        self.by_email: dict[str, str] = {}
        self.by_name: dict[str, str] = {}     # lowercase full name -> key
        self.tokens: dict[str, str] = {}      # lowercase name token -> key ("" if shared)
        self.extra_tokens: set[str] = set()   # tokens from spreadsheet name columns

    # ── learning ──
    def _key_for(self, name: str | None, email: str | None) -> str:
        email = (email or "").lower().strip()
        nm = " ".join((name or "").split()).strip(" '\"")
        if email and email in self.by_email:
            key = self.by_email[email]
        elif nm and nm.lower() in self.by_name:
            key = self.by_name[nm.lower()]
        elif nm and " " not in nm and self.tokens.get(nm.lower()):
            key = self.tokens[nm.lower()]  # "Marisol" = the Marisol Vega we already know
        else:
            key = f"p{len(self.labels) + 1}"
            self.labels[key] = f"Person {len(self.labels) + 1}"
            self.names[key], self.emails[key] = set(), set()
        if email:
            self.by_email[email] = key
            self.emails[key].add(email)
        if nm and len(nm) > 1 and "@" not in nm:
            self.by_name[nm.lower()] = key
            self.names[key].add(nm)
            from .rules import COMMON_WORDS

            for t in re.findall(r"[A-Za-z][A-Za-z'\-]+", nm):
                t = t.lower()
                # common words are not erased on their own ("Mr. Long" is still caught
                # as a full name; the word "long" elsewhere is left alone)
                # 3+ letters: two-letter parts ("La", "De") would erase state codes
                if len(t) < 3 or t in _NOT_SURNAME or t in COMMON_WORDS:
                    continue
                if t in self.tokens and self.tokens[t] != key:
                    self.tokens[t] = ""  # shared by two people: still removed, unlabeled
                else:
                    self.tokens[t] = key
        return key

    def add(self, name: str | None = None, email: str | None = None,
            trusted: bool = False) -> str:
        """trusted: the name came from a place that only holds people (a sign-off, a
        greeting, a title), so skip the is-it-a-company check."""
        if name:
            name = re.sub(r"\(.*?\)|\[.*?\]", "", name)
            if "," in name and "@" not in name:  # "Okafor, Darnell"
                last, _, first = name.partition(",")
                name = f"{first.strip()} {last.strip()}"
            # a display name that is really an address or a company
            if "@" in name or re.search(r"(?i)\b(llc|inc|corp|company|co\.|group|dept|"
                                        r"department|office|team|info|admin|billing|"
                                        r"support|accounts)\b", name):
                name = None
        if name and not trusted and not _looks_like_person(name, email):
            name = None  # "Crescent Lumber <orders@...>": a company, keep its name
        if not name and email:
            local = email.split("@")[0]
            parts = [p for p in re.split(r"[._\-]+", local) if p.isalpha() and len(p) > 1]
            if len(parts) >= 2 and parts[0].lower() in FIRST_NAMES:
                name = " ".join(p.capitalize() for p in parts[:2])
        return self._key_for(name, email)

    def add_header(self, value: str | None) -> list[str]:
        """'Darnell Okafor <d@x.com>; Marisol Vega <m@y.com>' -> labels."""
        out = []
        for part in re.split(r"[;\n]|,(?=[^>]*(?:<|$))", value or ""):
            part = part.strip()
            if not part:
                continue
            m = RE_ADDR.search(part)
            if m:
                out.append(self.add(m.group(1).strip(" '\"") or None, m.group(2)))
            elif re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", part):
                out.append(self.add(part, None))
        return [self.labels[k] for k in out]

    def learn_text(self, text: str) -> None:
        for m in RE_INLINE_ADDR.finditer(text):
            name = re.sub(r"(?i)^(?:from|to|cc|bcc|sent by|reply to)[ \t]*:?[ \t]*", "",
                          m.group(1)).strip(" ,.")
            words = [w for w in re.split(r"[ \t,]+", name) if w]
            if words and words[0].lower() in _NOT_SURNAME:
                words = words[1:]
            name = " ".join(words)
            if name and name.lower() not in COMMON_NAMES_BLOCK:
                self.add(name, m.group(2), trusted=len(words) <= 3)
        for m in RE_HEADER_LINE.finditer(text):
            self.add(m.group(1), trusted=True)
        for m in RE_SIGNOFF.finditer(text):
            self.add(" ".join(g for g in m.groups() if g), trusted=True)
        for m in RE_GREET.finditer(text):
            first, last = m.group(1), m.group(2)
            if first.lower() in FIRST_NAMES or first.lower() in self.tokens:
                self.add(f"{first} {last}" if last else first, trusted=True)
        for m in RE_TITLED.finditer(text):
            self.add(" ".join(g for g in m.groups() if g), trusted=True)
        for m in RE_FULL.finditer(text):
            first, last = m.group(1), m.group(2)
            if first.lower() in FIRST_NAMES and last.lower() not in _NOT_SURNAME \
                    and last.lower() not in FIRST_NAMES | {"llc", "inc"}:
                self.add(f"{first} {last}")

    # ── replacing ──
    def label_for_email(self, email: str) -> str:
        key = self.by_email.get(email.lower())
        return f"[{self.labels[key]} email]" if key else "[EMAIL]"

    def scrub(self, text: str, hits: dict | None = None) -> str:
        hits = hits if hits is not None else {}
        n = 0

        def _email(m):
            nonlocal n
            n += 1
            return self.label_for_email(m.group(0))

        from .rules import RE_EMAIL

        text = RE_EMAIL.sub(_email, text)
        # full names first (longest first), then single tokens
        for full in sorted(self.by_name, key=len, reverse=True):
            if " " not in full:
                continue
            rx = re.compile(r"\b" + r"\s+(?:[A-Z]\.\s+)?".join(map(re.escape, full.split()))
                            + r"\b", re.I)
            text, k = rx.subn(f"[{self.labels[self.by_name[full]]}]", text)
            n += k

        def _tok(m):
            nonlocal n
            w = m.group(0)
            lw = w.lower()
            key = self.tokens.get(lw)
            if key is None and lw not in self.extra_tokens:
                return w
            # single name words only when capitalized: "Mr. White" yes, "white paint" no
            if not w[0].isupper():
                return w
            n += 1
            return f"[{self.labels[key]}]" if key else "[NAME]"

        text = re.sub(r"(?<![\[\w])[A-Za-z][A-Za-z'\-]+(?![\w\]])", _tok, text)
        if n:
            hits["person"] = hits.get("person", 0) + n
        return text

    def __len__(self):
        return len(self.labels)

    def save(self, private_dir: str) -> str:
        path = os.path.join(private_dir, "people_key.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["label", "names_seen", "emails_seen"])
            for key, label in self.labels.items():
                w.writerow([label, "; ".join(sorted(self.names[key])),
                            "; ".join(sorted(self.emails[key]))])
        return path
