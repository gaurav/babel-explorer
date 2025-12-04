#
# Track progress/regressions with tests embedded in GitHub issues.
#
import dataclasses
import json
import logging
import re
from collections import defaultdict

from github import Github, Auth
import os

def split_normalization(result):
    return result.get('id', {'identifier': 'COULD NOT RESOLVE'})['identifier'], \
        result.get('id', {'label': 'COULD NOT RESOLVE'})['label'], \
        result.get('type', ['COULD NOT RESOLVE'])[0]


@dataclasses.dataclass(frozen=True)
class GitHubTest:
    issue_number: int
    issue_url: str
    issue_title: str
    test_string: str

    def components(self):
        test_string = self.test_string
        if test_string.startswith("{{BabelTest|"):
            test_string = self.test_string[12:]
        if test_string.endswith("}}"):
            test_string = test_string[:-2]
        return test_string.split("|")

class GitHubIssuesTracker:
    def __init__(self, nodenorm):
        self.logger = logging.getLogger(__name__)
        self.nodenorm = nodenorm

    def execute_issue_tests(self, repository="NCATSTranslator/Babel"):
        """
        Retrieve all GitHub issues from the specified repositories.

        Returns:
            list: A list of GitHub issues.
        """
        github = Github(auth=Auth.Token(os.getenv('GITHUB_PERSONAL_ACCESS_TOKEN')))
        repo = github.get_repo(repository)
        self.logger.info(f"Retrieving issues from {repository}: {repo}")

        issues_by_status = defaultdict(int)
        tests_by_status = defaultdict(int)
        actions = set()
        tests = set()

        issues = repo.get_issues(state='all', sort='updated')
        count_issue = 0
        for issue in issues:
            count_issue += 1
            if count_issue % 100 == 0:
                self.logger.info(f"Checked {count_issue} issues")

            if issue.pull_request:
                issues_by_status['PR'] += 1
                continue

            self.logger.debug(f" - Found issue #{issue.number} ({issue.html_url}): {issue} ({str(issue.state)})")
            issues_by_status[issue.state] += 1

            if issue.body and "{{BabelTest|" in issue.body:
                self.logger.info(f" - Found test(s) in issue #{issue.number} ({issue.html_url}): {issue.title}")
                pattern = r"\{\{BabelTest\|.*?\}\}"
                matches = re.findall(pattern, issue.body)
                for match in matches:
                    test = GitHubTest(
                        issue_number=issue.number,
                        issue_url=issue.html_url,
                        issue_title=issue.title,
                        test_string=match,
                    )
                    tests.add(test)

                    test_status = self.execute_test(test)
                    if test_status == 'SUCCESS' and issue.state == 'open':
                        # TODO: this should only occur if ALL the tests in a particular issue are marked as closed
                        actions.add(f"Issue #{issue.number:04d} ({issue.html_url}) can be closed")
                    elif test_status == 'FAIL' and issue.state == 'closed':
                        actions.add(f"Reopen issue #{issue.number:04d} ({issue.html_url}) should be reopened")

                    tests_by_status[test_status] += 1

        self.logger.info(f"Found {sum(issues_by_status.values())} issues/PRs in {repository}: {issues_by_status}")
        self.logger.info(f"Found {sum(tests_by_status.values())} tests in {repository}: {tests_by_status}")
        self.logger.info(f"Need to execute {len(actions)} actions: {json.dumps(sorted(actions), indent=2)}")

    def execute_test(self, test):
        self.logger.info(f" - {test} from #{test.issue_number} ({test.issue_url})")

        components = test.components()
        self.logger.debug(f"   - Components: {components}")
        curie1 = components[0]
        action = components[1]

        match action:
            case "Resolves":
                if len(components) > 2:
                    self.logger.error(f"   - Unexpected components: {components[2:]}")

                result = self.nodenorm.normalize_curie(curie1)
                if result is None:
                    self.logger.info(f"   - FAIL: Could not normalize {curie1}")
                    return 'FAIL'
                else:
                    resolved_to, label, biolink_type = split_normalization(result)
                    self.logger.info(f"   - SUCCESS: Normalized to {resolved_to} (\"{label}\", {biolink_type})")
                    return 'SUCCESS'

            case "ResolvesWith":
                if len(components) > 3:
                    self.logger.error(f"   - Unexpected components: {components[2:]}")
                curie2 = components[2]

                result1 = self.nodenorm.normalize_curie(curie1)
                resolved_to1, label1, biolink_type1 = split_normalization(result1)
                result2 = self.nodenorm.normalize_curie(curie2)
                resolved_to2, label2, biolink_type2 = split_normalization(result2)

                if resolved_to1 == resolved_to2:
                    self.logger.info(f"   - SUCCESS: both {curie1} and {curie2} resolved to {resolved_to1} (\"{label1}\", {biolink_type1})")
                    return 'SUCCESS'
                else:
                    self.logger.info(f"   - FAIL: {curie1} resolved to {resolved_to1} (\"{label1}\", {biolink_type1}) but {curie2} resolved to {resolved_to2} (\"{label2}\", {biolink_type2})")
                    return 'FAIL'

            case _:
                self.logger.error(f"   - Could not parse BabelTest: {test.test_string}.")
                return 'INVALID'

