# Economic calendar — hand-maintained (FOMC / CPI / NFP)

*Updated 2026-09-27. 2026-09-28 → 2027-12-31 for FOMC; CPI and Employment Situation through Dec 2026 (BLS publishes the 2027 schedule in December 2026 — refresh then).* Loaded into `saa.calendar_days.econ` by `./run.sh load-econ`; the daemon shows the day's rows in the 9:25 heartbeat and the brief reads them from the calendar row.

| Date | Time ET | Event | Source |
|---|---|---|---|
| 2026-10-02 | 08:30 | Employment Situation / NFP (Sep) | bls.gov |
| 2026-10-14 | 08:30 | CPI (Sep) | bls.gov |
| 2026-10-27 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2026-10-28 | 14:00 | FOMC decision (press conf 14:30) | federalreserve.gov |
| 2026-11-06 | 08:30 | Employment Situation / NFP (Oct) | bls.gov |
| 2026-11-10 | 08:30 | CPI (Oct) | bls.gov |
| 2026-12-04 | 08:30 | Employment Situation / NFP (Nov) | bls.gov |
| 2026-12-08 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2026-12-09 | 14:00 | FOMC decision + SEP/dot plot (press conf 14:30) | federalreserve.gov |
| 2026-12-10 | 08:30 | CPI (Nov) | bls.gov |
| 2027-01-26 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-01-27 | 14:00 | FOMC decision (press conf 14:30) | federalreserve.gov |
| 2027-03-16 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-03-17 | 14:00 | FOMC decision + SEP/dot plot (press conf 14:30) | federalreserve.gov |
| 2027-04-27 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-04-28 | 14:00 | FOMC decision (press conf 14:30) | federalreserve.gov |
| 2027-06-08 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-06-09 | 14:00 | FOMC decision + SEP/dot plot (press conf 14:30) | federalreserve.gov |
| 2027-07-27 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-07-28 | 14:00 | FOMC decision (press conf 14:30) | federalreserve.gov |
| 2027-09-14 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-09-15 | 14:00 | FOMC decision + SEP/dot plot (press conf 14:30) | federalreserve.gov |
| 2027-10-26 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-10-27 | 14:00 | FOMC decision (press conf 14:30) | federalreserve.gov |
| 2027-12-07 | — | FOMC day 1 (no decision) | federalreserve.gov |
| 2027-12-08 | 14:00 | FOMC decision + SEP/dot plot (press conf 14:30) | federalreserve.gov |

Sources: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm · https://www.bls.gov/schedule/news_release/cpi.htm · https://www.bls.gov/schedule/news_release/empsit.htm

Refresh: December 2026 (BLS 2027 CPI + Employment Situation schedules); FOMC 2028 when the Fed publishes it (usually mid-year).
