Feature: Arquivo de editais encerrados
  As someone looking for funding opportunities
  I want long-closed editais to move to a history folder
  So that the main listing shows what is open or just closed

  Background:
    Given an output folder with an edital closed 60 days ago, one closed 10 days ago, one closed without a deadline and one open

  Scenario: Old closed editais are archived
    When the daily refresh runs on 2026-11-26
    Then the edital closed 60 days ago is in the archive with the same name and content
    And the other three editais are still in the output folder

  Scenario: The report counts the archived editais
    When the daily refresh runs on 2026-11-26
    Then the refresh reports 1 archived edital

  Scenario: An edital past the grace that was still open is closed and archived in the same run
    Given an edital still open whose deadline was 45 days ago
    When the daily refresh runs on 2026-11-26
    Then that edital is in the archive as "encerrado"

  Scenario: The grace is configurable
    Given the archive grace is 5 days
    When the daily refresh runs on 2026-11-26
    Then the edital closed 10 days ago is also in the archive

  Scenario: Nothing is lost
    When the daily refresh runs on 2026-11-26
    Then the two folders together hold the same number of editais as before

  Scenario: The archive is frozen
    Given the archive already holds an edital the refresh would change
    When the daily refresh runs on 2026-11-26
    Then that archived edital is byte for byte the same

  Scenario: A reopened edital leaves the archive
    Given the archive holds an edital with link "https://exemplo/reaberto"
    When the sink writes an edital with link "https://exemplo/reaberto"
    Then the link "https://exemplo/reaberto" is only in the output folder

  Scenario: Writing other editais does not touch the archive
    Given the archive holds an edital with link "https://exemplo/reaberto"
    When the sink writes an edital with link "https://exemplo/outro"
    Then the archive still holds the link "https://exemplo/reaberto"
