Feature: Extração por IA com o Gemini
  As someone looking for funding opportunities
  I want the collector to read PDF editais with the free Gemini tier
  So that new editais keep reaching the portal without paid services

  Scenario: Gemini extracts an edital from PDF text
    Given the PDF text of an edital
    And a Gemini client that answers with the FAPES 10/2026 schedule
    When the Gemini service extracts the edital
    Then the edital keeps the schedule with the call of each stage
    And the request used the model gemini-3.5-flash-lite as JSON with temperature 0
    And the request carried the PDF text and the shared extraction instructions

  Scenario: PDF without text does not call the AI
    Given a PDF without extractable text
    And a Gemini client that answers with the FAPES 10/2026 schedule
    When the Gemini service extracts the edital
    Then no edital is returned
    And the AI was called 0 times

  Scenario: The FAPES 10/2026 schedule read by Gemini gives the current call
    Given the PDF text of an edital
    And a Gemini client that answers with the FAPES 10/2026 schedule
    When the normalizer processes the edital on 2026-11-25
    Then the closing date is 2026-12-15 and the opening date is 2026-10-14
    And the edital has 2 calls

  Scenario: Daily quota makes the AI unavailable for the rest of the run
    Given the PDF text of an edital
    And a Gemini client whose daily quota is exhausted
    When the Gemini service extracts the edital twice
    Then both extractions report the AI as unavailable because "cota diária esgotada"
    And the AI was called 1 times

  Scenario: Unavailable AI defers the item instead of rejecting it
    Given the AI is unavailable because "cota diária esgotada"
    When the normalizer processes a PDF edital on 2026-11-25
    Then no edital is returned
    And the edital URL is deferred
    And nothing is recorded as rejected

  Scenario: Daily quota in the middle of a batch defers the rest
    Given a CAPES batch of 5 PDF editais
    And a Gemini client whose daily quota runs out on call 3
    When the CAPES pipeline runs on 2026-11-25
    Then 2 editais are written
    And 3 editais are deferred and kept out of both registries
    And the flow stats report 3 deferred items

  Scenario: Missing key leaves the AI unavailable without breaking the flow
    Given no AI configuration
    When the extraction service is requested
    Then the service is unavailable because "chave do Gemini ausente"
