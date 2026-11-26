Feature: Horizon Europe sempre atualizado
  As someone looking for funding opportunities
  I want the Horizon Europe calls to reflect the official list every week
  So that postponed deadlines, openings and closings reach the portal

  Background:
    Given the Horizon calls A, B, C and D were collected last week

  Scenario: A postponed deadline reaches the saved file
    When the Horizon list now has call A with the deadline 2027-01-20
    Then call A has the closing date 2027-01-20 in the same file

  Scenario: A forthcoming call that opened is updated
    Given call F was collected while forthcoming
    When the Horizon list now has call F open since 2026-11-20
    Then call F has the opening date 2026-11-20

  Scenario: An unchanged call keeps its content
    When the Horizon list is the reference list
    Then call B has the same content as before

  Scenario: A new call is written and indexed
    When the Horizon list is the reference list
    Then call E is saved and indexed

  Scenario: A renamed call keeps a single file
    When the Horizon list is the reference list
    Then there is exactly one file for call D, with the new title

  Scenario: Existing file names do not change
    When the Horizon list has A, B, C and D unchanged
    Then the set of file names is the same as before

  Scenario: A call missing from the list is closed
    When the Horizon list is the reference list
    Then call C is closed and its file still exists

  Scenario: Incomplete dataset blocks closing
    When the Horizon list is the reference list cut in the middle
    Then call C is still open
    And the flow reports that closing was blocked

  Scenario: A sharp drop blocks closing
    When the Horizon list has only call A
    Then call C is still open
    And the flow reports that closing was blocked

  Scenario: A rejected call with a reopened deadline is published
    Given call G is in the active rejections
    When the Horizon list has call G with the deadline 2027-02-10
    Then call G is saved
    And the active rejections were not consulted

  Scenario: The run reports what changed
    When the Horizon list is the reference list
    Then the flow stats report created=1 updated=2 unchanged=1 closed=1

  Scenario: The daily refresh does not reopen a call closed by the list
    When the Horizon list is the reference list
    And the daily refresh runs on 2026-11-27
    Then call C is still closed
