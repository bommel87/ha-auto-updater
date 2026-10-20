# Auto-Updater

Installiert jede Nacht alle verfügbaren Updates und macht so lange weiter, bis keine mehr offen sind.
Das Add-on läuft in einem eigenen Container. Es übersteht deshalb Neustarts von Home Assistant und setzt den Lauf
nach einem OS-Update (Neustart des Hosts) automatisch fort.

## Ablauf pro Runde

1. Add-ons (mit Backup), Gerätefirmware, HACS
2. Home Assistant Core (mit Backup) – Core startet neu, das Add-on wartet
3. Home Assistant OS – der Host startet neu, danach geht es weiter
4. Gab es nur HACS-Updates, wird Core neu gestartet, damit sie aktiv werden

Danach beginnt die nächste Runde, bis nichts mehr offen ist oder das Zeitfenster endet.

- Jedes Update wird pro Nacht höchstens zweimal versucht.
- Was danach noch offen ist, kommt am Ende als Push-Nachricht.
- Übersprungene Updates (in HA „Überspringen“) werden ignoriert.
- Der Supervisor und das Add-on selbst werden nicht aktualisiert. Für dieses Add-on kannst du im Add-on-Store
  „Automatisch aktualisieren“ einschalten.

## Testen

1. **Testlauf** und **Beim Start ausführen** einschalten, speichern, Add-on neu starten.
   Du bekommst eine Push mit allem, was installiert würde. Es wird nichts installiert.
2. Beide Schalter wieder ausschalten.
3. Den Verlauf siehst du im Reiter **Protokoll** des Add-ons.

## Optionen

Siehe Reiter **Konfiguration** – jede Option ist dort beschrieben.
