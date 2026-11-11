# depot-kurse

Holt werktags etwa alle 30 Minuten die aktuellen EUR-Kurse für das Depot-Dashboard und legt sie in `quotes.json` ab.

- Quellen: justETF, onvista, Yahoo Finance; Ethereum über Coinbase (Ersatz CoinGecko)
- Steuerung: `.github/workflows/kurse.yml`, Abruf: `fetch_quotes.py`
- Manuell starten: Reiter **Actions** → **Kurse abrufen** → **Run workflow**

Die Datei enthält nur ISINs und Kurse, keine Stückzahlen oder Beträge.
