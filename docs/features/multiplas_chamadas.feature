Feature: Editais com várias chamadas
  As someone looking for funding opportunities
  I want each edital to show the deadline of the call that matters now
  So that an edital with an open call is never hidden as closed

  Scenario: The reference schedule is available
    Given the reference schedule of Edital FAPES 10/2026
    Then it should have 4 stages split into 2 calls

  Scenario Outline: Deadline and opening follow the current call
    Given the reference schedule of Edital FAPES 10/2026
    When the schedule fields are derived on <today>
    Then the closing date should be <closing>
    And the opening date should be <opening>
    And the status should be <status>

    Examples:
      | today      | closing    | opening    | status    |
      | 2026-11-25 | 2026-12-15 | 2026-10-14 | aberto    |
      | 2026-06-01 | 2026-07-07 | 2026-05-04 | aberto    |
      | 2027-01-01 | 2026-12-15 | 2026-10-14 | encerrado |

  Scenario: Daily refresh moves a stale deadline to the next call
    Given a saved edital with the reference schedule and the old deadline 2026-07-07
    When the daily refresh runs on 2026-11-25
    Then the saved closing date should be 2026-12-15
    And the saved opening date should be 2026-10-14
    And the saved status should be aberto

  Scenario Outline: Only real deadlines become the closing date
    Given a schedule with the single stage "<evento>" on "<data>"
    When the closing date is derived on 2026-01-01
    Then the closing date should be <esperado>

    Examples:
      | evento                               | data                    | esperado   |
      | Início da vigência dos projetos      | 2027-05-01              | none       |
      | Período de manifestação de interesse | 24/02/2026 a 02/03/2026 | 2026-03-02 |
      | Fim das inscrições                   | 2026-09-10              | 2026-09-10 |
      | Término das inscrições               | 2026-09-10              | 2026-09-10 |
      | Data limite para submissão           | 2026-09-10              | 2026-09-10 |
      | Submissão até                        | 2026-09-10              | 2026-09-10 |
      | Último dia para envio                | 2026-09-10              | 2026-09-10 |

  Scenario: Calls and next stage are published
    Given the reference schedule of Edital FAPES 10/2026
    When the schedule fields are derived on 2026-11-25
    Then the calls should be "1ª chamada 2026-05-04..2026-07-07; 2ª chamada 2026-10-14..2026-12-15"
    And the next stage should be "Encerramento da submissão" on 2026-12-15
