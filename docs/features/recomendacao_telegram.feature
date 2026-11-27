Feature: Recomendação semanal de editais a professores no Telegram
  As the research coordinator and the Telegram group
  I want a weekly message saying which professors fit each open edital
  So that good opportunities reach the right people in time

  Background:
    Given the professors of campus Serra from the Horizon fixture
    And the editais of the fixture

  Scenario: The gender edital recommends the sociologist and respects the degree
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then the message recommends "Ana Souza" for "VIOLÊNCIA DE GÊNERO"
    And "Carla Mendes" is not recommended for "VIOLÊNCIA DE GÊNERO"
    And the reason of "Ana Souza" mentions "Doutorado em Sociologia"

  Scenario: A master is not recommended for an edital that requires a doctorate
    Given the edital "finep_energia" requires "doutorado"
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then "Carla Mendes" is not recommended for "ENERGIA RENOVÁVEL"

  Scenario: A professor without Lattes comes with a warning
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then the message recommends "Diego Rocha" for "CAFÉ"
    And the message says "Diego Rocha (sem Lattes no Horizon: confirmar titulação)"

  Scenario: Short deadlines, closed editais and other campi stay out
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then the message does not mention "QUE FECHA LOGO"
    And the message does not mention "CHAMADA ENCERRADA"
    And the message does not mention "Fábio Outro"

  Scenario: Horizon calls go to their own section
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then the Horizon section comes after the Brazilian editais
    And the Horizon section has at most 5 editais

  Scenario: Sent editais do not come back and closing ones are reminded
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    And the weekly recommendation runs on 2026-12-09 with the Telegram group
    Then the second message says "Sem editais novos nesta semana."
    And the second message reminds "INTELIGÊNCIA ARTIFICIAL"

  Scenario: A large first batch is split into numbered parts
    Given 30 extra Brazilian editais and 50 extra Horizon calls about artificial intelligence
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then every part has at most 3900 characters and is numbered
    And the Horizon section has at most 5 editais

  Scenario: Without credentials nothing is sent
    When the weekly recommendation runs on 2026-11-30 without credentials
    Then nothing was sent and the exit code is 0
    And no edital is marked as sent

  Scenario: A Telegram failure keeps the editais for next week
    Given the Telegram group refuses the message
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then the exit code is 1
    And no edital is marked as sent

  Scenario: Horizon down without a previous copy sends nothing
    Given the Horizon export is unavailable and there is no previous copy
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then nothing was sent and the exit code is 1

  Scenario: Without AI quota the editais without vectors wait for next week
    Given the AI quota runs out after the professors
    When the weekly recommendation runs on 2026-11-30 with the Telegram group
    Then the message says "Sem editais novos nesta semana."
    And no edital is marked as sent
