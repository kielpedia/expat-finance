"""Generates fake statements in real bank export formats, for trying the app. Run: python make_samples.py"""
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(7)
HERE = Path(__file__).parent
start = date(2025, 11, 1)
days = [start + timedelta(d) for d in range(365)]


def nl(v):
    return f"{abs(v):.2f}".replace(".", ",")


# ---------------- ING joint checking (semicolon, newest first, as ING exports)
rows, bal = [], 4200.0
for d in days:
    tx = []
    if d.day == 25:
        tx += [("ACME BV", "Salaris " + d.strftime("%B"), 5400.0), ("Partner Employer BV", "Salaris", 2300.0)]
    if d.day == 1:
        tx += [("Vesteda Woningbeheer", "Huur " + d.strftime("%m-%Y"), -1950.0),
               ("Zilveren Kruis", "Zorgverzekering", -298.0), ("Vattenfall", "Termijnbedrag energie", -165.0),
               ("Ziggo", "Internet en TV", -58.0), ("Partou Kinderopvang", "Kinderopvang maand", -1480.0)]
    if d.day == 2:
        tx += [("Oranje Spaarrekening", "Naar Oranje Spaarrekening", -800.0)]
    if d.day == 5:
        tx += [("Videoland", "Videoland abonnement", -9.99), ("Spotify", "Spotify P0F2", -17.99)]
    if d.day == 15:
        tx += [("Gemeente Voorbeeldstad", "Gemeentelijke belastingen termijn", -62.0)]
    if d.day == 7 and d.month in (1, 4, 7, 10):
        tx += [("Sociale Verzekeringsbank", "Kinderbijslag", 653.37)]
    if d.day == 20:
        tx += [("DEGIRO", "Storting DEGIRO", -300.0)]
    if random.random() < 0.55:
        tx += [(random.choice(["Albert Heijn 1432", "Jumbo", "Lidl", "Picnic"]), "Betaalautomaat", -round(random.uniform(12, 95), 2))]
    if random.random() < 0.18:
        tx += [(random.choice(["Thuisbezorgd.nl", "Eetcafe De Kroon", "Starbucks"]), "Betaalautomaat", -round(random.uniform(9, 70), 2))]
    if random.random() < 0.12:
        tx += [(random.choice(["NS Groep", "Shell", "Q-Park"]), "Betaalautomaat", -round(random.uniform(5, 80), 2))]
    if random.random() < 0.08:
        tx += [(random.choice(["Bol.com", "HEMA", "Kruidvat", "Coolblue"]), "Betaalautomaat", -round(random.uniform(8, 160), 2))]
    for name, mem, amt in tx:
        bal += amt
        rows.append((d, name, mem, amt, bal))

lines = ['"Datum";"Naam / Omschrijving";"Rekening";"Tegenrekening";"Code";"Af Bij";"Bedrag (EUR)";"Mutatiesoort";"Mededelingen";"Saldo na mutatie";"Tag"']
for d, name, mem, amt, b in reversed(rows):
    lines.append(f'"{d:%Y%m%d}";"{name}";"NL12INGB0001234567";"";"BA";"{"Af" if amt < 0 else "Bij"}";"{nl(amt)}";"Betaalautomaat";"{mem}";"{nl(b)}";""')
(HERE / "ing_joint_checking.csv").write_text("\n".join(lines), encoding="utf-8")

# ---------------- Rabobank savings
lines = ['"IBAN/BBAN","Munt","BIC","Volgnr","Datum","Rentedatum","Bedrag","Saldo na trn","Tegenrekening IBAN/BBAN","Naam tegenpartij","Omschrijving-1","Omschrijving-2"']
bal = 38000.0
for i, d in enumerate([x for x in days if x.day == 2]):
    bal += 800
    lines.append(f'"NL99RABO0123456789","EUR","RABONL2U","{i+1:018d}","{d:%Y-%m-%d}","{d:%Y-%m-%d}","+800,00","{nl(bal)}","NL12INGB0001234567","Eigen rekening","Maandelijkse inleg",""')
    if d.month == 1:
        bal += 410.5
        lines.append(f'"NL99RABO0123456789","EUR","RABONL2U","{i+100:018d}","{d:%Y-%m-%d}","{d:%Y-%m-%d}","+410,50","{nl(bal)}","","Rabobank","Rente","2025"')
(HERE / "rabobank_savings.csv").write_text("\n".join(lines), encoding="cp1252")

# ---------------- Chase checking (US)
lines = ["Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #"]
bal = 14100.0
out = []
for d in days:
    tx = []
    if d.day == 3:
        tx += [("VERIZON WIRELESS PAYMENT", -45.0), ("STATE FARM INSURANCE", -38.0)]
    if d.day == 10 and d.month in (2, 8):
        tx += [("VANGUARD BUY INVESTMENT", -2000.0)]
    if d.day == 28:
        tx += [("AMEX EPAYMENT ACH PMT", -round(random.uniform(150, 400), 2))]
    if d == date(2026, 4, 20):
        tx += [("IRS TREAS 310 TAX REF", 1250.0)]
    for desc, amt in tx:
        bal += amt
        out.append(f"{'CREDIT' if amt > 0 else 'DEBIT'},{d:%m/%d/%Y},{desc},{amt:.2f},ACH_DEBIT,{bal:.2f},")
lines += list(reversed(out))
(HERE / "chase_checking.csv").write_text("\n".join(lines), encoding="utf-8")

# ---------------- Amex (charges positive)
lines = ["Date,Description,Amount,Extended Details,Appears On Your Statement As,Address,City/State,Zip Code,Country,Reference,Category"]
for d in days:
    if random.random() < 0.06:
        desc, amt = random.choice([("AMAZON.COM", 42.1), ("APPLE.COM/BILL", 9.99), ("UNITED AIRLINES", 780.0), ("BOOKING.COM", 312.0)])
        lines.append(f"{d:%m/%d/%Y},{desc},{amt:.2f},,,,,,,'{random.randint(10**9, 10**10)}',")
    if d.day == 28:
        lines.append(f"{d:%m/%d/%Y},AUTOPAY PAYMENT - THANK YOU,-250.00,,,,,,,,")
(HERE / "amex.csv").write_text("\n".join(lines), encoding="utf-8")
print("Wrote", *[p.name for p in HERE.glob("*.csv")])
