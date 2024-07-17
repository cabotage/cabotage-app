# Contributing to Cabotage

👋 **First off! Hello, and a warm welcome to the cabotage project!**

We’re delighted that you’re interested in participating, and we want to make it as pleasant and simple as we can, whether you want to send a one-off pull request or you want to become part of the team in the long run.

**If you’re new here and interested in any form of contribution, this
guide is for you.**

Please take a moment to review this document to make the contribution process easy and effective for everyone involved.

Following these guidelines helps to communicate that you respect the time of the developers managing and developing this open source project. In return, they should reciprocate that respect by addressing your issue, assessing changes, and helping you finalize your pull requests.

- [Getting started](#getting-started)
  - [Decide what you want to work on](#decide-what-you-want-to-work-on)
  - [Notify your interest](#notify-your-interest)
  - [Setting up the project in your local machine](#setting-up-the-project-in-your-local-machine)
  - [Local setup and testing](#local-setup-and-testing)
  - [Code formatting](#code-formatting)
- [Pull requests](#pull-requests)
  - [Everyone can contribute](#everyone-can-contribute)
  - [I have submitted my Pull Request, what are the next steps?](#i-have-submitted-my-pull-request-what-are-the-next-steps)
- [Reporting bugs](#reporting-bugs)
- [Disclose a security vulnerability](#disclose-a-security-vulnerability)
- [Get in touch](#get-in-touch)


## Getting started

### Decide what you want to work on

If you are looking for something to work on, you can look through our [issues](https://github.com/cabotage/cabotage-app/issues) and pick some you like. We try to maintain a list of issues that should be suitable for first time contributions, they can be found tagged [`good first contribution`](https://github.com/cabotage/cabotage-app/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22).

If you’re still unsure, please contact us and we will help you to the best of our abilities.


### Notify your interest

Please let us know if you want to work on it so we can avoid multiple people working on the same issue. You can leave a comment on the issue tagging the author to notify your interest to work on the issue.

Before starting any large pull requests (like adding features or reworking the code), **please ask first**. If you don’t, you run the risk of investing a lot of time in something that the project’s developers might decide not to include.


### Setting up the project on your local machine

1. [Fork](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/fork-a-repo) the project, clone your fork, and configure the remotes:

    ```sh
    # Clone your fork of the repo into the current directory
    git clone https://github.com/<your-username>/<repo-name>

    # Navigate to the newly cloned directory
    cd <repo-name>

    # Assign the original repo to a remote called "upstream"
    git remote add upstream https://github.com/cabotage/cabotage-app
    ```

2. Create a new topic branch to contain your feature, change, or fix:

    ```sh
    git checkout -b <topic-branch-name>
    ```

3. Write clear and meaningful git commit messages.
    We use the imperative mood as a coding convention, which typically follows this structure:

    ```sh
    <imperative verb> <description>

    [optional body]
    ```

    Separate the subject from the body with a blank line and use the body to explain what changed and why.

    To find some examples, you can checkout the style of the commit messages on our [main branch](https://github.com/cabotage/cabotage-app/commits/main/).

4. Make sure to update or add to the tests when appropriate. Run the appropriate testing suites to check that all tests pass after you’ve made changes.

5. If you added or changed a feature, make sure to document it accordingly in the [README.md](./README.md) file, when appropriate.


### Local setup and testing

Make sure to add tests for any new features or improvements made to the code. Details on local setup and running tests can be found in the [README.md](./README.md) file.


### Code formatting
Before committing changes, you can lint and reformat your code. Please ensure you have [Docker](https://www.docker.com/) and [Compose](https://docs.docker.com/compose/) installed, and then run:

```sh
make lint
make reformat
```


## Pull requests

Good pull requests - patches, improvements, new features - are a fantastic help. They should remain focused in scope and avoid containing unrelated commits.

1. Update your branch to the latest changes in the upstream main branch, solving conflicts if any appear. You can do that locally with:

    ```sh
    git pull --rebase upstream main
    ```

2. Push your topic branch up to your fork:

    ```sh
    git push origin <topic-branch-name>
    ```

3. [Open a Pull Request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request) with a clear title and a detailed description explaining the reasons for the changes. Make sure there is sufficient information for the reviewer to understand your changes.

4. Check if the CI/CD pipelines have passed. Address the errors if they have not.

> [!IMPORTANT]  
> By submitting a patch, you agree to license your work under the same license as that used by the project.


### Everyone can contribute

An Open Source project such as this one has a wide range of needs, and there are a lot of opportunities to help:

- You can expand or improve the test suite
- You can improve the various documentation resources in general, just picking something to expand upon, edit, or check for typos
- You can evangelize for the project: present or talk about Cabotage App in your company, local tech meetup, YouTube channel, or similar circles


### I have submitted my Pull Request, what are the next steps?

First of all, :hugs:  thank you for your contribution! Sit back and relax :coffee: 

As soon as possible, usually within a few weeks, a team member will review your pull request and provide comments.


## Reporting bugs

A bug is a _demonstrable problem_ that is caused by the code in our repository. Good bug reports are extremely helpful!

A well-written bug report shouldn’t require others to contact you directly to obtain additional details. Kindly ensure that your report contains as much detail as you can. What surrounds you? In what ways can the problem be replicated? Which operating system is affected by the issue? What result would you anticipate? All of these specifics will aid in the correction of any possible bugs.

Before you create a bug report, you should:

1. **Use the GitHub issue search** - check if the issue has already been reported.

2. **Check if the issue has been fixed** - try to reproduce it using the latest `main` branch in the repository.

3. **Isolate the problem** - ideally create a reduced test case.

To create a new bug report, add a new [issue](https://github.com/cabotage/cabotage-app/issues).


## Disclose a security vulnerability

If you've identified a security issue, please do not report it publicly on GitHub issues or any other public form. Instead, navigate to GitHub's Security tab and click on Report a vulnerability. Fill out [the form](https://github.com/cabotage/cabotage-app/security/advisories/new), providing as much relevant information as possible, including steps to reproduce the issue. Thank you for helping us keep our project secure!


## Get in touch

Please feel free to reach out to us on our [Github discussions](https://github.com/cabotage/cabotage-app/discussions)

