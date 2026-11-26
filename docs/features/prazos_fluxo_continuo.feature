Feature: Prazos lidos do edital ou da página; sem prazo, fluxo contínuo
  As someone looking for funding on the portal
  I want every open call to show its deadline, or to say I can apply at any time
  So that "open without a deadline" no longer hides wrong documents or unread pages

  Scenario: The original edital is read before its amendment
    Given the real CAPES page "catedra_jorge_amado" with the edital text "Inscrições até 10/12/2026." and the amendment text "Prorroga as inscrições até 20/12/2026."
    And the AI answers the closing date 2026-12-20
    When the CAPES item is read and normalized on 2026-11-26
    Then the AI was called once
    And the AI received the original edital before the amendment and then the page
    And the edital is published with the closing date 2026-12-20 and no modality

  Scenario: A page without an edital is read instead of the menu
    Given the real CAPES page "premio_capes_tese"
    And the AI answers without a closing date
    When the CAPES item is read and normalized on 2026-11-26
    Then the description starts with "O Prêmio CAPES de Tese reconhece"
    And the AI received only the page text
    And the edital is published as "fluxo-contínuo" with origin "sem-prazo"

  Scenario: A notice that is only on the page goes with the edital
    Given the real CAPES page "programa_aurora" with the edital text "Ciclos mensais de inscrição." and the amendment text "Altera o item 4."
    And the AI answers without a closing date
    When the CAPES item is read and normalized on 2026-11-26
    Then the AI received the original edital before the amendment and then the page
    And the AI text contains "ficam encerrados os ciclos mensais"

  Scenario: An unreadable edital is an error, not a continuous flow
    Given the real CAPES page "catedra_jorge_amado" whose edital download fails
    When the CAPES item is transformed on 2026-11-26
    Then nothing is published
    And the item error is "DocumentReadError"
    And the AI was not called

  Scenario: A written continuous flow is kept even if the AI does not say so
    Given the real CAPES page "premio_capes_tese"
    And the page text says "Propostas recebidas em fluxo contínuo."
    And the AI answers without a closing date and without the declaration
    When the CAPES item is read and normalized on 2026-11-26
    Then the edital is published as "fluxo-contínuo" with origin "declarada"

  Scenario: A supporting document is refused
    Given a FAPES document "Diretrizes FAPES para participação no Horizonte Europa"
    And the AI classifies it as a supporting document
    When it is normalized on 2026-11-26
    Then nothing is published
    And the refusal reason is "documento de apoio, não é um edital"

  Scenario: FINEP reads the edital when the API has no deadline
    Given the FINEP call "EDITAL FIP NORDESTE CAPITAL SEMENTE" without a deadline and with its real attachments
    And the AI answers the closing date 2026-12-30
    When the FINEP call is read and normalized on 2026-11-26
    Then the AI was called once
    And the FINEP category classifier was not called
    And the edital is published with the closing date 2026-12-30 and no modality
    And the published name is the API name

  Scenario: FINEP edital without a deadline becomes a continuous flow
    Given the FINEP call "EDITAL FIP NORDESTE CAPITAL SEMENTE" without a deadline and with its real attachments
    And the AI answers without a closing date
    When the FINEP call is read and normalized on 2026-11-26
    Then the edital is published as "fluxo-contínuo" with origin "sem-prazo"

  Scenario: FINEP with a deadline in the API does not read the edital
    Given the FINEP call "EDITAL FIP NORDESTE CAPITAL SEMENTE" with the API deadline 2026-12-15 and with its real attachments
    When the FINEP call is read and normalized on 2026-11-26
    Then the AI was not called
    And the edital is published with the closing date 2026-12-15 and no modality

  Scenario: Only continuous flows without a deadline are reread after 7 days
    Given the portal output has open continuous flows read 8 days ago, 3 days ago, declared, and never recorded
    When the daily runner selects the rereads on 2026-11-26
    Then only the ones read 8 days ago, never recorded, or open without deadline and modality are selected

  Scenario: A reread that finds a deadline leaves the index
    Given the index has "https://exemplo/edital" as "sem-prazo"
    When the item is persisted again with a deadline
    Then the index no longer has "https://exemplo/edital"
