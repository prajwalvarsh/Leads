# Spot check — Task 1

- Seed: **20261109** (20 random `confirmed` rows, `random.Random(seed).sample` over companies ordered by `company_id`)
- Check model: `openai/gpt-4o-mini` with its own prompt (`prompts/check_v1.md`), own text extraction (plain selectolax), Impressum fetched fresh (no cache). The model never saw the sheet values.
- **Caveat:** the check model is the same family as `models.extract` (both `openai/gpt-4o-mini`, user choice). The spec asks for a different family, so shared blind spots are possible. A first run with `anthropic/claude-haiku-5.5` (not kept, user chose gpt-4o-mini) gave the same pipeline errors plus one more, listed under *Also found*.
- Rows resolved by the investigate agent: none (agent not run).
- Raw results: `eval/spotcheck_results.json`. Rerun: `python -m pipeline.cli spotcheck --params '{"seed": 20261109}'`.

## Mismatches (7) — review these

| row | field | sheet value | check value | who is wrong | evidence / finding | fix |
|---|---|---|---|---|---|---|
| 2 | is_property_manager | yes | no | Check model | Check saw only the Impressum. Pipeline quote from the home page: "Unser Fokus liegt auf der Entwicklung und Verwaltung von Mehrfamilienhäusern." Pipeline right. | Spot check: give the check model the home/about page too, not just the Impressum. |
| 7 | is_property_manager | yes | no | Check model | Same: the Impressum says nothing about the business. Pipeline quote: "Wir bieten Ihnen eine Immobilienverwaltung, wie sie sein sollte…". Pipeline right. | Same as above. |
| 8 | street | Marienburger Str. 1 | Marienburger Str. 1 • 10405 Berlin | Check model | Check value includes the postcode ("Marienburger Str. 1 • 10405 Berlin"). Sheet value is correct. | None for the pipeline; spot-check comparison could cut at "•". |
| 8 | register_number |  | HR-B 124081 B | **Pipeline** | Impressum has "Handelsregister: HR-B 124081 B". The register pattern does not accept the hyphen in "HR-B", so regex missed it, and the LLM value was not accepted either. | Allow `HR-A`/`HR-B` in the register pattern (`normalize._REG`), rerun extract (cached, free). |
| 16 | company_name | Hausverwaltung B&G | Hausverwaltung B&G - Barbara Kuhl-Dunkel e. K. | **Pipeline** | Impressum name is "Hausverwaltung B&G - Barbara Kuhl-Dunkel e. K.". The LLM's name was rejected by the evidence check (its quote joined two lines), so the sheet fell back to the shorter directory name. | When the LLM name fails the evidence check, retry the quote check line by line, or take the Impressum line that contains the directory name; legal form e.K. is lost otherwise. |
| 18 | register_number |  | GSR59B | **Pipeline** | Impressum has "GSR59B" (Gesellschaftsregister for an eGbR, new since 2024). Not a format the pipeline knows. | Add `GsR` to the register types in `normalize._REG`. |
| 20 | company_name | Sven Kurtzke Immobilienfachwirt IHK | Kurtzke Immobilien | Both partly | Sheet has "Sven Kurtzke Immobilienfachwirt IHK" (name plus job title); check took the trade name "Kurtzke Immobilien". The owner is "Sven Kurtzke", trading as Kurtzke Immobilien. | Extract prompt v2: "do not include professional titles (Immobilienfachwirt IHK, Dipl.-Ing.) in company_name". |

### Also found (first run, Claude Haiku 5.5 as check model)

| row | field | sheet | check | finding | fix |
|---|---|---|---|---|---|
| 5 | is_property_manager | yes | no | werneburg-immobilien.de is an estate agent: the site says "Immobilienmakler Berlin – Seit 1996" throughout. The pipeline LLM said yes from a sentence about Makler helping WEGs, and the keep-word "mietverwalt" appeared on the page, so LLM and rules agreed wrongly. **Pipeline wrong.** gpt-4o-mini as check model also said yes. | Classify prompt v3: "if the site presents itself mainly as Makler / Immobilienmakler, answer no even if it mentions Verwaltung"; and do not count keep-words that appear only inside a sentence about another business. |

## Accuracy per field

| field | match | with pipeline errors only* |
|---|---|---|
| company_name | 18/20 (90%) | 18/20 (90%) |
| street | 19/20 (95%) | 20/20 (100%) |
| postcode | 20/20 (100%) | 20/20 (100%) |
| phone | 20/20 (100%) | 20/20 (100%) |
| register_number | 18/20 (90%) | 18/20 (90%) |
| is_property_manager | 18/20 (90%) | 19/20 (95%) |

\* counting only the rows where the pipeline value is wrong (check-model errors removed, Werneburg added).

## Full table

| row | field | sheet value | check value | match | evidence quote |
|---|---|---|---|---|---|
| 1 | company_name | MFG Services GmbH | MFG Services GmbH | yes | MFG Services GmbH |
| 1 | street | Patschkauer Weg 35 | Patschkauer Weg 35 | yes | Patschkauer Weg 35 |
| 1 | postcode | 14195 | 14195 | yes | 14195 |
| 1 | phone | +49308312122 | +49 30 8312122 | yes | Tel: +49 30 8312122 |
| 1 | register_number | Amtsgericht Charlottenburg HRB 15621 | HRB 15621 | yes | Handelsregisternummer: 96 HRB 15621 |
| 1 | is_property_manager | yes | yes | yes | Die MFG Services GmbH Hausverwaltung |
| 2 | company_name | R.E.M.M. Invest Gesellschaft mit beschränkter Haftung | R.E.M.M. Invest Gesellschaft mit beschränkter Haftung | yes | R.E.M.M. Invest Gesellschaft mit beschränkter Haftung |
| 2 | street | Saarbrücker Straße 36 | Saarbrücker Straße 36 | yes | Saarbrücker Straße 36 |
| 2 | postcode | 10405 | 10405 | yes | 10405 |
| 2 | phone | +49304466910 | +49 30 446691-0 | yes | Tel: +49 30 446691-0 |
| 2 | register_number | Amtsgericht Charlottenburg HRB 198985 | HRB 198985 | yes | HRB 198985 |
| 2 | is_property_manager | yes | no | no |  |
| 3 | company_name | Hausverwaltung Küntzel | Hausverwaltung Küntzel | yes | Hausverwaltung Küntzel |
| 3 | street | Habichtstr. 6a | Habichtstr. 6a | yes | Habichtstr. 6a |
| 3 | postcode | 13505 | 13505 | yes | 13505 |
| 3 | phone | +493041744245 | +49(0)30-417 44 245 | yes | Tel.: +49(0)30-417 44 245 |
| 3 | register_number |  |  | yes |  |
| 3 | is_property_manager | yes | yes | yes | Seit 1970 in der Haus- und Grundstücksverwaltung in Berlin und seit 1999 in Brandenburg tätig |
| 4 | company_name | Urban Hausverwaltung UH GmbH | Urban Hausverwaltung UH GmbH | yes | Urban Hausverwaltung UH GmbH |
| 4 | street | Zähringerstr. 28 | Zähringerstr. 28 | yes | Zähringerstr. 28 |
| 4 | postcode | 10707 | 10707 | yes | 10707 Berlin |
| 4 | phone | +493086329180 | 030 / 863 29 18 0 | yes | Telefon: 030 / 863 29 18 0 |
| 4 | register_number | Amtsgericht Berlin (Charlottenburg) HRB 207305 B | HRB 207305 B | yes | Registernummer: HRB 207305 B |
| 4 | is_property_manager | yes | yes | yes | Erlaubnis als Wohnimmobilienverwalter gemäß § 34c Abs. 1 Satz 1 Nr. 4 GewO |
| 5 | company_name | Werneburg Immobilien | Werneburg Immobilien | yes | Werneburg Immobilien |
| 5 | street | Marientaler Straße 24a | Marientaler Straße 24a | yes | Marientaler Straße 24a |
| 5 | postcode | 12437 | 12437 | yes | 12437 |
| 5 | phone | +493066655666 | +49 (0) 30 666 55 666 | yes | Telefon: +49 (0) 30 666 55 666 |
| 5 | register_number |  |  | yes |  |
| 5 | is_property_manager | yes | yes | yes | Die Gewerbeerlaubnis nach § 34c GewO zur Vermittlung des Abschlusses und zum Nachweis der Gelegenheit zum Abschluss von  |
| 6 | company_name | Plura Haus und Immobilien GmbH | Plura Haus und Immobilien GmbH | yes | Plura Haus und Immobilien GmbH |
| 6 | street | Mariendorfer Damm 298 | Mariendorfer Damm 298 | yes | Mariendorfer Damm 298 |
| 6 | postcode | 12107 | 12107 | yes | 12107 Berlin |
| 6 | phone | +49307730020 | 030 / 773 00 20 | yes | Telefon: 030 / 773 00 20 |
| 6 | register_number | HRB 80934 | HRB 80934 | yes | HRB 80934 |
| 6 | is_property_manager | yes | yes | yes | Die hier dargestellten Immobilien / Inserate werden über die Maklersoftware von OpenEstate durch die Plura Haus und Immo |
| 7 | company_name | Schimpf GmbH | SCHIMPF GmbH | yes | SCHIMPF GmbH |
| 7 | street | Dernburgstr. 59 | Dernburgstr. 59 | yes | Dernburgstr. 59 |
| 7 | postcode | 14057 | 14057 | yes | 14057 |
| 7 | phone | +49303214073 | 030 / 3214073 | yes | Telefon: 030 / 3214073 |
| 7 | register_number | Amtsgericht Charlottenburg HRB 842 | HRB 842 | yes | Handelsregister: HRB 842 |
| 7 | is_property_manager | yes | no | no |  |
| 8 | company_name | Merkur Immobilienverwaltung UG (haftungsbeschränkt) | Merkur Immobilienverwaltung UG (haftungsbeschränkt) | yes | Merkur Immobilienverwaltung UG (haftungsbeschränkt) |
| 8 | street | Marienburger Str. 1 | Marienburger Str. 1 • 10405 Berlin | no | Marienburger Str. 1 • 10405 Berlin |
| 8 | postcode | 10405 | 10405 | yes | Marienburger Str. 1 • 10405 Berlin |
| 8 | phone | +493044310672 | 030 - 44 31 06 72 | yes | Telefon: 030 - 44 31 06 72 |
| 8 | register_number |  | HR-B 124081 B | no | Handelsregister: HR-B 124081 B |
| 8 | is_property_manager | yes | yes | yes | Hausverwaltung |
| 9 | company_name | Hausverwaltung Paukstat GmbH | Hausverwaltung Paukstat GmbH | yes | Hausverwaltung Paukstat GmbH |
| 9 | street | Schloßstraße 21 | Schloßstraße 21 | yes | Schloßstraße 21 |
| 9 | postcode | 13507 | 13507 | yes | 13507 |
| 9 | phone | +491796151770 | +491796151770 | yes | Telefon: +491796151770 |
| 9 | register_number | Amtsgericht Charlottenburg (Berlin) HRB 243612 B | HRB 243612 B | yes | Registernummer: HRB 243612 B |
| 9 | is_property_manager | yes | yes | yes | Ihre Hausverwaltung mit Herz! |
| 10 | company_name | Heinz Ziggel KG | Heinz Ziggel KG | yes | Heinz Ziggel KG |
| 10 | street | Caspar-Theyß-Str. 14 A | Caspar-Theyß-Str. 14 A | yes | Caspar-Theyß-Str. 14 A |
| 10 | postcode | 14193 | 14193 | yes | 14193 |
| 10 | phone | +49303921047 | +49 (0) 30 - 39 21 04 7 | yes | Telefon: +49 (0) 30 - 39 21 04 7 |
| 10 | register_number | Amtsgericht Charlottenburg HRA 15967 B | HRA 15967 B | yes | HRA 15967 B |
| 10 | is_property_manager | yes | yes | yes | Erlaubnis nach §34c der Gewerbeordnung |
| 11 | company_name | rentatect GmbH | rentatect GmbH | yes | rentatect GmbH |
| 11 | street | Voigtstraße 36/37 | Voigtstraße 36/37 | yes | Adresse: Voigtstraße 36/37 |
| 11 | postcode | 10247 | 10247 | yes | 10247 Berlin |
| 11 | phone | +493034620569 | 030 - 34 62 05 69 | yes | Telefon: 030 - 34 62 05 69 |
| 11 | register_number | Amtsgericht Berlin-Charlottenburg HRB 139700 B | HRB 139700B | yes | Handelsregister: Amtsgericht Berlin-Charlottenburg, HRB 139700B |
| 11 | is_property_manager | yes | yes | yes | GmbH Immobilien & Verwaltungen |
| 12 | company_name | G. Gawehn Grundstücksverwaltung GmbH | G. Gawehn Grundstücksverwaltung GmbH | yes | G. Gawehn Grundstücksverwaltung GmbH |
| 12 | street | Peter-Vischer-Str. 2 | Peter-Vischer-Str. 2 | yes | Peter-Vischer-Str. 2 |
| 12 | postcode | 12157 | 12157 | yes | 12157 Berlin |
| 12 | phone | +49308557145 | +49 30 855 71 45 | yes | Telefon: +49 30 855 71 45 |
| 12 | register_number | Amtsgericht Charlottenburg HRB 46330 | HRB 46330 Amtsgericht Charlottenburg | yes | Handelsregister: HRB 46330 Amtsgericht Charlottenburg |
| 12 | is_property_manager | yes | yes | yes | G. Gawehn Grundstücksverwaltung GmbH |
| 13 | company_name | AM Group GmbH & Co. KG | AM Group GmbH & Co. KG | yes | AM Group GmbH & Co. KG |
| 13 | street | Groß-Ziethener Chaussee 133 | Groß-Ziethener Chaussee 133 | yes | Groß-Ziethener Chaussee 133 |
| 13 | postcode | 12355 | 12355 | yes | 12355 |
| 13 | phone | +493064462002 | +49 30 64 46 20 02 | yes | Tel.: +49 30 64 46 20 02 |
| 13 | register_number | Amtsgericht Berlin HRA 60364 B | HRA 60364 B | yes | Registernummer: HRA 60364 B |
| 13 | is_property_manager | yes | yes | yes | Angaben für die Tätigkeit als Immobilienmakler/in gemäß § 34c Abs. 1 GewO |
| 14 | company_name | meterhoch2! Hausverwaltungen GmbH | meterhoch2! Hausverwaltungen GmbH | yes | meterhoch2! Hausverwaltungen GmbH |
| 14 | street | Schwarzbacher Straße 3 | Schwarzbacher Straße 3 | yes | Schwarzbacher Straße 3 |
| 14 | postcode | 10711 | 10711 | yes | 10711 |
| 14 | phone | +493089090903 | +49(0)30 89090903 | yes | Telefon: +49(0)30 89090903 |
| 14 | register_number | Amtsgericht Berlin-Charlottenburg HRB 51361 B | HRB 51361B | yes | Handelsregister: HRB 51361B |
| 14 | is_property_manager | yes | yes | yes | meterhoch2! Hausverwaltungen GmbH |
| 15 | company_name | Wotum Gesellschaft für Wohnungseigentum mbH | Wotum Gesellschaft für Wohnungseigentum mbH | yes | Wotum Gesellschaft für Wohnungseigentum mbH |
| 15 | street | Heinrich-Heine-Str. 62D | Heinrich-Heine-Str. 62D | yes | Heinrich-Heine-Str. 62D |
| 15 | postcode | 10179 | 10179 | yes | 10179 Berlin |
| 15 | phone | +49304436390 | (030) 44 36 39 – 0 | yes | Telefon: (030) 44 36 39 – 0 |
| 15 | register_number | AG Berlin-Charlottenburg HRB 55018 | HRB 55018 | yes | Registernummer: HRB 55018 |
| 15 | is_property_manager | yes | yes | yes | Wotum Gesellschaft für Wohnungseigentum mbH |
| 16 | company_name | Hausverwaltung B&G | Hausverwaltung B&G - Barbara Kuhl-Dunkel e. K. | no | Hausverwaltung B&G - Barbara Kuhl-Dunkel e. K. |
| 16 | street | Aachener Straße 44 | Aachener Straße 44 | yes | Anschrift: Aachener Straße 44 |
| 16 | postcode | 10713 | 10713 | yes | Aachener Straße 44 |
| 16 | phone | +493036742479 | +49 (0)30 367 424 79 | yes | Telefon: +49 (0)30 367 424 79 |
| 16 | register_number | HRA 56914 B | HRA 56914 B | yes | Handelsregister-Eintrag: HRA 56914 B |
| 16 | is_property_manager | yes | yes | yes | Wohnimmobilienverwaltung gem. §34 c Abs.1 Nr. 4 GewO. |
| 17 | company_name | Eßer Hausverwaltung | Eßer Hausverwaltung | yes | Eßer Hausverwaltung |
| 17 | street | Pücklerstr. 23 | Pücklerstr. 23 | yes | Pücklerstr. 23 |
| 17 | postcode | 10997 | 10997 | yes | 10997 |
| 17 | phone | +49306159163 | 030 - 615 91 63 | yes | Tel: 030 - 615 91 63 |
| 17 | register_number |  |  | yes |  |
| 17 | is_property_manager | yes | yes | yes | Eßer Hausverwaltung |
| 18 | company_name | Bersulin eGbR | Bersulin eGbR | yes | Bersulin eGbR |
| 18 | street | Bundesallee 181 | Bundesallee 181 | yes | Bundesallee 181 |
| 18 | postcode | 10717 | 10717 | yes | 10717 |
| 18 | phone | +49308543022 | 030 8543022 | yes | Telefon: 030 8543022 |
| 18 | register_number |  | GSR59B | no | Registereintrag: GSR59B |
| 18 | is_property_manager | yes | yes | yes | Verwaltung von 350 Mieteinheiten in Berlin und am Bodensee |
| 19 | company_name | Terra Real Verwaltungs GmbH | Terra Real Verwaltungs GmbH | yes | Terra Real Verwaltungs GmbH |
| 19 | street | Sybelstr 32 | Sybelstr 32 | yes | Sybelstr 32 |
| 19 | postcode | 10629 | 10629 | yes | 10629 |
| 19 | phone | +49303240845 | +49 30 324 08 45 | yes | Telefon: +49 30 324 08 45 |
| 19 | register_number | Amtsgericht Berlin-Charlottenburg HRB 51417 | HRB 51417 | yes | Registernummer: 93 HRB 51417 |
| 19 | is_property_manager | yes | yes | yes |  |
| 20 | company_name | Sven Kurtzke Immobilienfachwirt IHK | Kurtzke Immobilien | no | KURTZKE IMMOBILIEN – Ihr Immobilienbegleiter |
| 20 | street | Langhansstr. 83/83a | Langhansstr. 83/83a | yes | Langhansstr. 83/83a, 13086 Berlin |
| 20 | postcode | 13086 | 13086 | yes | Langhansstr. 83/83a, 13086 Berlin |
| 20 | phone | +493088725320 | 030 88725320 | yes | Tel 030.88725320 |
| 20 | register_number |  |  | yes |  |
| 20 | is_property_manager | yes | yes | yes | zertifizierter Verwalter IHK im Sinn von § 26a WEG |
