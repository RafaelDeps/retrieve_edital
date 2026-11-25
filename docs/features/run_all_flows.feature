Feature: Runner unificado dos fluxos
  As an ETL operator
  I want to execute every daily source from a single runner
  So that one broken source never costs the data collected by the others

  Scenario: Default run executes the seven daily flows in order
    Given every flow finishes successfully
    When the runner executes without a flow filter
    Then it should invoke FAPES, FINEP, CONIF, PRPPG_IFES, PROEX_IFES, CAPES and CNPQ in this order
    And the runner should exit with code 0

  Scenario: A failing flow does not stop the others and the run reports failure
    Given the flow "FINEP" fails and every other flow succeeds
    When the runner executes without a flow filter
    Then all seven daily flows should have been invoked
    And the runner should exit with code 1

  Scenario: The job summary lists every flow and highlights failures
    Given the job summary file is available
    And the flow "FINEP" fails and every other flow succeeds
    When the runner executes without a flow filter
    Then the job summary should have one row per daily flow
    And the job summary should highlight "FINEP" as failed
    And the runner should exit with code 1

  Scenario: A flow that hangs without output is killed at the time limit
    Given a flow that sleeps for 30 seconds without printing anything
    When the runner executes it with a time limit of 1 second
    Then the flow should be killed with the timeout exit code
    And it should take less than 10 seconds

  Scenario Outline: Time limit precedence
    Given the time limit option is "<cli>" and the environment variable is "<env>"
    When the runner resolves the time limit
    Then the time limit should be <expected> seconds

    Examples:
      | cli  | env  | expected |
      | none | none | 2400     |
      | none | 120  | 120      |
      | 60   | 120  | 60       |

  Scenario: HORIZON runs only when selected by filter
    When the flows are selected without a filter
    Then "HORIZON" should not be selected
    When the flows are selected with the filter "horizon,finep"
    Then the selected flows should be "FINEP,HORIZON"
