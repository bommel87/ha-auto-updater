# Changelog

## 1.0.4

- Neue Option „Marketplace-Hinweis bestätigt“: Seit 2026.11 gilt die Bestätigung pro Benutzer, auch für den Systembenutzer des Add-ons. Mit der Option bestätigt das Add-on den Hinweis beim Start selbst.

## 1.0.3

- Unterstützt den Marketplace aus Home Assistant 2026.11 (früher HACS): Updates werden wieder erkannt und HA danach neu gestartet.
- Lehnt HA ein Update ab, steht der Grund in Protokoll und Push, z. B. wenn der Marketplace-Hinweis noch nicht bestätigt ist.
- Abgelehnte Updates blockieren den Lauf nicht mehr 90 Sekunden.

## 1.0.2

- Firmware, die im Hintergrund installiert wird (z. B. Shelly), wird nicht mehr fälschlich als fehlgeschlagen gemeldet.
- Laufende Updates werden nicht ein zweites Mal angestoßen.

## 1.0.1

- Start ohne s6-Init, damit die Verbindung zu Home Assistant klappt.
- Protokoll zeigt, warum Home Assistant nicht erreichbar ist.

## 1.0.0

- Erste Version: nächtliche Updates für HACS, Core, OS, Add-ons und Gerätefirmware mit Push bei Fehlern.
