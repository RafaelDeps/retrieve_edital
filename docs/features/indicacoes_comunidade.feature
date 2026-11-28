Feature: Indicações de editais pela comunidade
  As someone who suggests an edital on the portal
  I want the collector to read my suggestion, keep only funding opportunities and answer me
  So that good suggestions reach the portal and the others are explained

  Scenario: A funding suggestion is published and answered
    Given the portal has the suggestion "issue_fomento" pointing to a funding edital
    When the community flow runs on 2026-11-26 with the reply token
    Then the edital is published with source "comunidade" and the community tag
    And suggestion 101 is answered with "✅ Publicado no portal" and closed

  Scenario: The form fills only what the document lacks
    Given the portal has the suggestion "issue_fomento" pointing to an edital without agency or deadline
    When the community flow runs on 2026-11-26 with the reply token
    Then the published edital has the agency "FAPES" and the deadline 2026-12-15 from the form

  Scenario: An expired suggested edital is refused
    Given the portal has the suggestion "issue_fomento" pointing to an expired funding edital
    When the community flow runs on 2026-11-26 with the reply token
    Then nothing is published
    And suggestion 101 is answered with "❌ Não publicado: prazo encerrado" and closed

  Scenario: The Banestes suggestion is refused without AI
    Given the portal has the real suggestion "issue_banestes" pointing to the Banestes page
    When the community flow runs on 2026-11-26 with the reply token
    Then the AI was not called
    And suggestion 15 is answered with "❌ Não publicado: fora do tema: concurso público" and closed

  Scenario: The AI refuses an off-topic suggestion
    Given the portal has the suggestion "issue_fomento" pointing to an edital the AI finds off topic
    When the community flow runs on 2026-11-26 with the reply token
    Then nothing is published
    And suggestion 101 is answered with "❌ Não publicado: fora do tema: premiação cultural sem relação com ciência" and closed

  Scenario: A fellowship selection is not blocked by the word filter
    Given the portal has the suggestion "issue_bolsistas" pointing to a funding edital
    When the community flow runs on 2026-11-26 with the reply token
    Then the AI was called once

  Scenario: A link already in the portal is not read
    Given the portal has the suggestion "issue_fomento" pointing to an edital already in the portal
    When the community flow runs on 2026-11-26 with the reply token
    Then the link was not fetched
    And suggestion 101 is answered with "✅ Já está no portal" and closed

  Scenario: Only the cap is read per run
    Given the portal has 15 funding suggestions
    When the community flow runs on 2026-11-26 with the reply token
    Then 10 links were fetched

  Scenario: Invalid and unreachable links are refused
    Given the portal has a suggestion without a link and one with a link that is down
    When the community flow runs on 2026-11-26 with the reply token
    Then suggestion 103 is answered with "❌ Não publicado: link inválido" and closed
    And suggestion 104 is answered with "❌ Não publicado: link inacessível" and closed

  Scenario: Issues that are not suggestions are ignored
    Given the portal has only a regular issue
    When the community flow runs on 2026-11-26 with the reply token
    Then the link was not fetched
    And no suggestion was answered

  Scenario: Without the token nothing is answered
    Given the portal has the suggestion "issue_fomento" pointing to a funding edital
    When the community flow runs on 2026-11-26 without the reply token
    Then the edital is published with source "comunidade" and the community tag
    And no suggestion was answered

  Scenario: A failing reply does not undo the publication
    Given the portal has the suggestion "issue_fomento" pointing to a funding edital
    And answering on the portal fails
    When the community flow runs on 2026-11-26 with the reply token
    Then the edital is published with source "comunidade" and the community tag

  Scenario: A suggestion refused before the reply token existed is answered later
    Given the portal has the real suggestion "issue_banestes" pointing to the Banestes page
    And the suggestion was refused on an earlier run without the reply token
    When the community flow runs on 2026-11-28 with the reply token
    Then the link was not fetched
    And the AI was not called
    And suggestion 15 is answered with "❌ Não publicado: fora do tema: concurso público" and closed
