"""
Rule-based categorisation. Rules are case-insensitive regexes matched against
counterparty + description. Your own rules (Settings page) run before the defaults.
Transactions you categorise by hand are locked and never re-categorised.
"""
import re


# Category names use "Group: Sub" so reports can roll up by group.
DEFAULT_RULES = [
    # ---- income
    (r"schwab: (qualified dividend|non-qualified div|cash dividend|special dividend|bank interest|credit interest|qual div reinvest|pr yr)", "Income: Interest & dividends"),
    (r"schwab: (buy|sell|reinvest shares|reinvest dividend|journal|moneylink|wire|stock split|spin-?off|internal transfer|security transfer|acat|transfer of security|tod transfer|rollover|delivered|received|deposit of shares)", "Transfer: Investing"),
    (r"schwab: (adr mgmt fee|service fee|foreign tax paid)", "Fees"),
    (r"schwab: nra tax|schwab: .*withholding", "Taxes"),
    (r"salaris|salary|\bloon\b|nettoloon|payroll|direct dep", "Income: Salary"),
    (r"\bsvb\b|sociale verzekeringsbank|kinderbijslag", "Income: Kinderbijslag"),
    # toeslagen: one category each, so a missing one can be spotted even when another arrives
    (r"kinderopvangtoeslag|\bkot\b.*toeslag", "Income: Kinderopvangtoeslag"),
    (r"kindgebonden|\bkgb\b", "Income: Kindgebonden budget"),
    (r"zorgtoeslag", "Income: Zorgtoeslag"),
    (r"huurtoeslag", "Income: Huurtoeslag"),
    (r"toeslagen", "Income: Toeslagen (other)"),
    (r"belastingdienst.*(teruggaaf|restitutie|refund)|irs treas.*tax ref", "Income: Tax refund"),
    (r"\brente\b|interest paid|interest earned|interest income|bank interest|credit interest|schwab bank.*interest|dividend",
     "Income: Interest & dividends"),
    # ---- transfers between own accounts (excluded from budget)
    # Wise only counts as a transfer when it's clearly your own money moving (a top-up or a conversion);
    # paying someone through Wise is spending and falls through to the rules below.
    (r"(wise|transferwise).*(top.?up|topped up|opwaarde|own account|eigen rekening|convert|balance|jar\b)|"
     r"(top.?up|topped up|opwaarde).*(wise|transferwise)|^converted [\d.,]+ [a-z]{3} to [\d.,]+ [a-z]{3}", "Transfer"),
    (r"eigen rekening|own account|spaarrekening|savings transfer|oranje spaar|transfer (to|from)|online transfer|zelle to self", "Transfer"),
    (r"degiro|meesman|brand new day|peaks|bux|saxo|trading 212|vanguard|schwab|fidelity|etrade|robinhood", "Transfer: Investing"),
    (r"credit ?card payment|creditcard|\bics\b|international card services|autopay|epayment|payment thank you", "Transfer: Card payment"),
    # ---- taxes
    (r"belastingdienst|irs |us treasury|gemeente.*belasting|gemeentelijke belasting|waterschap|bghu|\bgbt\b|svhw|bsgw", "Taxes"),
    # ---- subscriptions: before groceries so "Disney Plus" isn't read as the PLUS supermarket
    (r"netflix|spotify|disney|videoland|hbo|max\.com|apple\.com|icloud|google storage|youtube|amazon prime|nlziet|chatgpt|anthropic|claude\.ai", "Subscriptions"),
    # ---- EV charging from energy companies: before utilities
    (r"vattenfall incharge|shell recharge|eneco emobility|fastned|allego", "Transport: Fuel & charging"),
    # ---- housing
    (r"hypotheek|mortgage", "Housing: Mortgage"),
    (r"\bhuur\b|\brent\b|verhuur|woningcorporatie|vesteda|mn wonen", "Housing: Rent"),
    (r"vve|vereniging van eigenaren", "Housing: HOA / VvE"),
    (r"vattenfall|eneco|essent|greenchoice|budget energie|energiedirect|vandebron|frank energie|tibber|vitens|evides|brabant water|pwn|dunea|oasen|waternet", "Housing: Utilities"),
    (r"ziggo|kpn|odido|t-mobile|vodafone|verizon|at&t|\batt\b|google fi|mint mobile|simyo|lebara|tele2|youfone|\bben\b|delta fiber", "Housing: Internet & phone"),
    (r"gamma|praxis|karwei|hornbach|ikea|\baction\b", "Housing: Home & DIY"),
    # ---- family
    (r"kinderopvang|kdv|bso|gastouder|partou|kindergarden|smallsteps|humankind|kinderdagverblijf|daycare", "Family: Childcare"),
    (r"school|ouderbijdrage|overblijf|\btso\b", "Family: School"),
    (r"zeeman|hema|kruidvat|prenatal|babypark|intertoys|bart smit|lego", "Family: Kids & household"),
    # pets: before Insurance/Health so "dierenziekenhuis" or an insurer's pet policy lands here
    (r"zooplus|pets ?place|\bjumper\b|zoomania|welkoop|discus dier|dierenspeciaal|dierenwinkel|dierenarts|dierenkliniek|"
     r"dierenziekenhuis|dierenpension|kattenpension|hondenschool|hondenuitlaat|trimsalon|dierenverzekering|petsecur|"
     r"petco|petsmart|chewy|banfield|vca animal|bluepearl|trupanion|\bvet(erinary)?\b", "Family: Pets"),
    # ---- food
    (r"albert heijn|\bah\b|jumbo|lidl|aldi|\bplus\b|\bdirk\b|dekamarkt|vomar|\bcoop\b|\bspar\b|picnic|crisp|ekoplaza|costco|whole foods|trader joe|safeway|kroger|walmart grocery", "Food: Groceries"),
    (r"thuisbezorgd|uber ?eats|deliveroo|restaurant|cafe|eetcafe|starbucks|mcdonald|burger king|kfc|subway|doordash|grubhub|febo|new york pizza|domino", "Food: Dining & takeaway"),
    # ---- transport
    # bikes: before public transport and shopping so bike shops and repairs land here
    (r"fietsenmaker|fietsenwinkel|fietsenzaak|fietshersteller|fietsreparatie|fiets ?onderhoud|fietsplus|rijwiel|tweewieler|"
     r"bike ?totaal|bike ?shop|bikeshop|halfords|profile.{0,10}fiets|swapfiets|vanmoof|stella fiets|bike repair|bicycle", "Transport: Bike maintenance"),
    (r"\bns\b|ns groep|ov-chipkaart|ovpay|gvb|\bret\b|htm|arriva|connexxion|qbuzz|keolis|\bebs\b|\bkeolis\b|uber(?! ?eats)|bolt", "Transport: Public & taxi"),
    (r"shell|\bbp\b|esso|tango|tinq|\btotal(energies)?\b|texaco|argos|fastned|allego|vattenfall incharge|shell recharge", "Transport: Fuel & charging"),
    (r"anwb|parkeren|fietsenstalling|q-park|yellowbrick|parkmobile|easypark|rdw", "Transport: Car & parking"),
    (r"klm|transavia|easyjet|ryanair|united airlines|delta air|american airlines|booking\.com|airbnb|hotel|expedia", "Travel"),
    # ---- health & insurance
    (r"zilveren kruis|\bcz\b|menzis|vgz|ditzo|ohra|fbto|zekur|interpolis|\bnn\b|nationale-nederlanden|centraal beheer|univé|unive|a\.s\.r|\basr\b|allianz|aegon|geico|state farm", "Insurance"),
    (r"apotheek|huisarts|tandarts|fysio|ziekenhuis|pharmacy|cvs|walgreens|dental", "Health"),
    # ---- shopping & subscriptions
    (r"bol\.com|amazon|coolblue|mediamarkt|zalando|h&m|primark|uniqlo|decathlon|wehkamp|target|best buy", "Shopping"),
    # ---- cash & fees
    (r"atm fee rebate|atmrebate|atm rebate", "Fees"),
    (r"geldautomaat|geldmaat|\batm\b|cash withdrawal|opname", "Cash"),
    (r"kosten|\bfees?\b|provisie|bankkosten|pakket|foreign transaction", "Fees"),
]

GROUPS_EXCLUDED_FROM_BUDGET = ("Transfer",)


def check_pattern(pattern: str) -> str | None:
    """None if the pattern is a usable regex, else a short reason."""
    if not pattern or not str(pattern).strip():
        return "empty pattern"
    if len(pattern) > 300:
        return "pattern longer than 300 characters"
    try:
        re.compile(pattern, re.I)
    except re.error as e:
        return f"not a valid pattern: {e}"
    return None


def load_rules(con):
    user = [(r["pattern"], r["category"]) for r in
            con.execute("SELECT pattern, category FROM cat_rules ORDER BY priority, id")]
    out = []
    for p, c in user:
        if check_pattern(p) is None:      # a broken saved rule is skipped instead of breaking every page
            out.append((re.compile(p, re.I), c))
    return out + [(re.compile(p, re.I), c) for p, c in DEFAULT_RULES]


def categorize_text(text: str, rules) -> str:
    for rx, cat in rules:
        if rx.search(text):
            return cat
    return "Uncategorized"


def apply(con, only_uncategorized=False, only_categories=None) -> int:
    rules = load_rules(con)
    q = "SELECT id, description, counterparty, amount FROM transactions WHERE category_locked=0"
    params = []
    if only_uncategorized:
        q += " AND (category IS NULL OR category='Uncategorized')"
    if only_categories:
        q += f" AND category IN ({','.join('?' * len(only_categories))})"
        params = list(only_categories)
    n = 0
    for r in con.execute(q, params).fetchall():
        text = f"{r['counterparty'] or ''} {r['description'] or ''}"
        cat = categorize_text(text, rules)
        if cat == "Uncategorized" and r["amount"] > 0:
            cat = "Income: Other"
        con.execute("UPDATE transactions SET category=? WHERE id=?", (cat, r["id"]))
        n += 1
    con.commit()
    return n


def group_of(cat: str) -> str:
    return (cat or "Uncategorized").split(":")[0].strip()


def is_budget(cat: str) -> bool:
    return group_of(cat) not in GROUPS_EXCLUDED_FROM_BUDGET


def all_categories(con) -> list[str]:
    cats = {c for _, c in DEFAULT_RULES} | {"Uncategorized", "Income: Other"}
    cats |= {r["category"] for r in con.execute("SELECT DISTINCT category FROM transactions WHERE category IS NOT NULL")}
    cats |= {r["category"] for r in con.execute("SELECT category FROM cat_rules")}
    return sorted(cats)
