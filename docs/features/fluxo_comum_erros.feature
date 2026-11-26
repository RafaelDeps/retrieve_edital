Feature: Erros registrados e roteiro comum dos fluxos
  As the person who maintains the collector
  I want items that fail to transform to be recorded and visible
  So that the same error is not retried and paid for on every run

  Scenario: A failing item is recorded and the valid one is written
    Given a CONIF batch where item A fails with ValueError and item B is valid
    When the CONIF flow runs on 2026-11-26
    Then item B is written
    And item A is in the rejected index as "erro na transformação: ValueError" until 2026-11-29

  Scenario: The run reports the errors
    Given a CONIF batch where item A fails with ValueError and item B is valid
    When the CONIF flow runs on 2026-11-26
    Then the flow stats report "errors=1 error_types=ValueError:1"
    And the runner result is "Atenção"
    And the runner note says "1 itens com erro na transformação (ValueError: 1); ficam fora por 3 dias."

  Scenario: A failing item is not read again within its validity
    Given a CONIF batch where item A fails with ValueError and item B is valid
    When the CONIF flow runs on 2026-11-26
    And the CONIF flow runs again on 2026-11-27
    Then the source of the second run already knew item A

  Scenario: After its validity the item is tried again
    Given a CONIF batch where item A fails with ValueError and item B is valid
    When the CONIF flow runs on 2026-11-26
    And the CONIF flow runs again on 2026-11-30
    Then the source of the second run did not know item A

  Scenario: The validity is configurable
    Given the transform error validity is 5 days
    And a CONIF batch where item A fails with ValueError and item B is valid
    When the CONIF flow runs on 2026-11-26
    Then item A is in the rejected index as "erro na transformação: ValueError" until 2026-12-01

  Scenario: A deferred item is not an error
    Given a CONIF batch where item A is deferred and item B is valid
    When the CONIF flow runs on 2026-11-26
    Then item A is not in the rejected index
    And the flow stats report "deferred=1"
    And the flow stats do not report errors

  Scenario: When every item fails the flow is a warning, not an empty source
    Given a CONIF batch where items A and B fail with ValueError
    When the CONIF flow runs on 2026-11-26
    Then the runner result is "Atenção"
    And the runner note says "2 itens com erro na transformação (ValueError: 2)"
    And the runner note does not say "verificar se o portal mudou"
