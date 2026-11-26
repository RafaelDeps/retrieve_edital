Feature: CAPES e CNPq de volta no GitHub
  As someone looking for funding opportunities
  I want CAPES and CNPq to be collected even when www.gov.br refuses the GitHub servers
  So that their new editais keep reaching the portal

  Scenario: CNPq through Jina matches direct
    Given www.gov.br answers the CNPq pages directly
    When the CNPq source reads the calls
    And www.gov.br refuses the connection and the CNPq source reads the calls again
    Then both reads give the same editais
    And the read through Jina carries the PDF as text

  Scenario: CAPES through Jina matches direct
    Given www.gov.br answers the CAPES pages directly
    When the CAPES source reads the calls
    And www.gov.br refuses the connection and the CAPES source reads the calls again
    Then both reads give the same editais
    And the read through Jina carries the PDF as text

  Scenario: The block is remembered
    Given www.gov.br refuses the connection to the CNPq pages
    When the CNPq source reads the calls
    Then the direct connection was tried only once

  Scenario: A PDF text from Jina reaches the AI
    Given an edital whose PDF arrived as text
    When the normalizer processes it on 2026-11-26
    Then the AI received that text

  Scenario: Direct access never calls Jina
    Given www.gov.br answers the CNPq pages directly
    When the CNPq source reads the calls
    Then Jina was not called

  Scenario: An HTTP error does not trigger the detour
    Given www.gov.br answers the CNPq listing but the call page is missing
    When the CNPq source reads the calls
    Then Jina was not called
    And no call was read

  Scenario: The detour is a success with a note
    Given www.gov.br refuses the connection to the CNPq pages
    When the CNPq flow runs and the runner reads its stats
    Then the result is "Sucesso"
    And the note says "páginas buscadas pelo Jina"

  Scenario: Direct and detour both failed
    Given www.gov.br refuses the connection and Jina is rate limited
    When the CNPq flow runs and the runner reads its stats
    Then the result is "Atenção"
    And the note says "Falha de rede: www.gov.br recusou a conexão e o desvio pelo Jina falhou (jina_429)"
    And the note does not say "verificar se o portal mudou"

  Scenario: An empty source is still reported as empty
    Given www.gov.br answers an empty CNPq listing directly
    When the CNPq flow runs and the runner reads its stats
    Then the result is "Atenção"
    And the note says "verificar se o portal mudou"
    And the note does not say "Falha de rede"

  Scenario: Jina asks to wait and then answers
    Given www.gov.br refuses the connection and Jina asks to wait 5 seconds once
    When the CNPq source reads the calls
    Then the CNPq call was read
    And the fetcher waited 5 seconds
