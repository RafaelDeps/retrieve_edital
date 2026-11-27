Feature: Elegibilidade dos editais para recomendação
  As the portal and the weekly recommendation
  I want each edital to say who can apply (degree, areas, audience, requirements)
  So that editais can be matched with a Lattes curriculum

  Scenario: A doctorate requirement in health is read with the edital
    Given an edital text read by the AI with "doutorado", areas "Saúde" and audience "docente, pesquisador"
    When it is normalized on 2026-11-26
    Then the AI was called once
    And the eligibility has degree "doutorado" and areas "Ciências da Saúde"
    And the audience is "pesquisador, docente"
    And the requirements summary is "Coordenador doutor com vínculo em ICT."

  Scenario: An edital open to any area gets the nine areas
    Given an edital text read by the AI with "", areas "Todas as áreas" and audience ""
    When it is normalized on 2026-11-26
    Then the eligibility has 9 areas

  Scenario: An edital without explicit requirements has an empty eligibility
    Given an edital text read by the AI without eligibility fields
    When it is normalized on 2026-11-26
    Then the eligibility is present and empty

  Scenario: Values outside the vocabularies are dropped
    Given an edital text read by the AI with "pós-doutorado", areas "Ciência da Computação, Engenharia" and audience "aluno, docente"
    When it is normalized on 2026-11-26
    Then the eligibility has degree "" and areas "Engenharias"
    And the audience is "docente"

  Scenario: A source without a document is not read and has no eligibility
    Given a PRPPG item without a document but with a deadline
    When it is normalized on 2026-11-26
    Then the eligibility is null
    And the AI was not called

  Scenario: The audience declared by the source wins over the document
    Given a FINEP call with a deadline, the API audience "ict" and the short read audience "docente"
    When it is normalized on 2026-11-26
    Then the audience is "empresa, ict"

  Scenario: Horizon gets the eligibility from a short read
    Given a Horizon call without a saved eligibility
    And the short read answers areas "Engenharias"
    When it is normalized on 2026-11-26
    Then the short read was called once
    And the eligibility has degree "" and areas "Engenharias"
    And the audience is "pesquisador"

  Scenario: Horizon keeps what was already read
    Given a Horizon call with a saved eligibility for areas "Ciências Agrárias"
    When it is normalized on 2026-11-26
    Then the short read was not called
    And the eligibility has degree "" and areas "Ciências Agrárias"

  Scenario: FINEP uses one call for category and eligibility
    Given a FINEP call with a deadline, the API audience "" and the short read audience "pesquisador"
    When it is normalized on 2026-11-26
    Then the short read was called once
    And the FINEP category classifier was not called
    And the category is "inovação"

  Scenario: Without AI quota the item is published without eligibility
    Given a Horizon call without a saved eligibility
    And the AI has no quota left
    When it is normalized on 2026-11-26
    Then the edital is published with a null eligibility
    And the item is pending eligibility

  Scenario: The runner warns about pending eligibility
    Given a flow reported 3 items pending eligibility
    When the runner reads the stats
    Then the result is Atenção with "3 item(ns) sem elegibilidade"

  Scenario: The daily reread completes the archive with a cap
    Given the output has 3 open editais without eligibility, 1 with it and 1 Horizon without it
    When the runner selects the eligibility rereads with a cap of 2
    Then the 2 with the nearest deadline are selected and Horizon is not
