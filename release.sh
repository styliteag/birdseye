#!/bin/bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

show_usage() {
    echo "Usage: $0 [major|minor|patch]"
    echo ""
    echo "This script will:"
    echo "  1. Update CHANGELOG.md (move [Unreleased] to new version)"
    echo "  2. Increment the version in ./VERSION file"
    echo "  3. Update pyproject.toml version"
    echo "  4. Run uv sync + ruff check as build verification"
    echo "  5. Commit the version change on the current branch"
    echo "  6. Create an annotated git tag v<version>"
    echo "  7. Push the branch and tag to origin"
    echo ""
    echo "Version increment types (patch is default):"
    echo "  major: X.0.0 (breaking changes)"
    echo "  minor: X.Y.0 (new features, backward compatible)"
    echo "  patch: X.Y.Z (bug fixes, backward compatible)"
    echo ""
    echo "Current version: $(cat VERSION 2>/dev/null || echo 'VERSION file not found')"
}

increment_version() {
    local current_version="$1"
    local increment_type="$2"

    IFS='.' read -r -a version_parts <<< "$current_version"
    local major="${version_parts[0]}"
    local minor="${version_parts[1]}"
    local patch="${version_parts[2]}"

    case $increment_type in
        major) major=$((major + 1)); minor=0; patch=0 ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        patch) patch=$((patch + 1)) ;;
        *)
            print_error "Invalid increment type: $increment_type"
            return 1
            ;;
    esac

    echo "$major.$minor.$patch"
}

check_git_status() {
    print_info "Checking git status..."

    if ! git diff-index --quiet HEAD --; then
        print_error "Working directory is not clean. Please commit or stash your changes first."
        git status --short
        exit 1
    fi

    local untracked_files
    untracked_files=$(git ls-files --others --exclude-standard)
    if [ -n "$untracked_files" ]; then
        print_warning "Untracked files found:"
        echo "$untracked_files" | sed 's/^/  /'
        read -p "Continue with untracked files? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi

    local current_branch
    current_branch=$(git branch --show-current)
    if [[ ! "$current_branch" =~ ^(main|master|develop)$ ]]; then
        print_warning "Current branch is '$current_branch'. Releases are usually cut from main/master/develop."
        read -p "Continue anyway? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi

    print_success "Git status is clean"
}

update_changelog() {
    local new_version="$1"
    local current_date
    current_date=$(date +%Y-%m-%d)

    print_info "Updating CHANGELOG.md..."

    if [ ! -f "CHANGELOG.md" ]; then
        print_warning "CHANGELOG.md not found, skipping changelog update"
        return 0
    fi

    if ! grep -q "## \[Unreleased\]" CHANGELOG.md; then
        print_warning "No [Unreleased] section found in CHANGELOG.md, skipping"
        return 0
    fi

    sed -i.bak "s/## \[Unreleased\]/## [$new_version] - $current_date/" CHANGELOG.md
    rm -f CHANGELOG.md.bak

    local temp_changelog
    temp_changelog=$(mktemp)
    {
        echo "# Changelog"
        echo ""
        echo "All notable changes to birdseye will be documented in this file."
        echo ""
        echo "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),"
        echo "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)."
        echo ""
        echo "## [Unreleased]"
        echo ""
        echo "### Added"
        echo "-"
        echo ""
    } > "$temp_changelog"

    sed -n '/## \['"$new_version"'\]/,$p' CHANGELOG.md >> "$temp_changelog"
    mv "$temp_changelog" CHANGELOG.md

    print_success "CHANGELOG.md updated for version $new_version"
}

run_build_tests() {
    print_info "Running build verification..."

    if [ ! -f "pyproject.toml" ] || [ ! -f "docker/Dockerfile" ]; then
        print_error "Not in project root (expected pyproject.toml and docker/Dockerfile)."
        exit 1
    fi

    print_info "Resolving deps with uv sync..."
    if ! uv sync > /tmp/uv-sync.log 2>&1; then
        print_error "uv sync failed!"
        cat /tmp/uv-sync.log
        exit 1
    fi
    print_success "Dependencies resolved"

    print_info "Linting with ruff..."
    if ! uv run ruff check . > /tmp/ruff.log 2>&1; then
        print_error "ruff check failed!"
        cat /tmp/ruff.log
        exit 1
    fi
    print_success "ruff check passed"

    print_success "All build checks passed"
}

main() {
    if [ $# -eq 0 ]; then
        local increment_type="patch"
    elif [ $# -eq 1 ]; then
        local increment_type="$1"
    else
        show_usage
        exit 1
    fi

    if [[ ! "$increment_type" =~ ^(major|minor|patch)$ ]]; then
        print_error "Invalid increment type: $increment_type"
        show_usage
        exit 1
    fi

    if [ ! -f "VERSION" ]; then
        print_error "VERSION file not found in current directory"
        exit 1
    fi

    check_git_status

    local current_version
    current_version=$(cat VERSION | tr -d '\n')
    print_info "Current version: $current_version"

    local new_version
    new_version=$(increment_version "$current_version" "$increment_type")
    print_info "New version: $new_version"

    update_changelog "$new_version"

    local current_branch
    current_branch=$(git branch --show-current)
    print_info "Updating VERSION file to $new_version on $current_branch"
    echo "$new_version" > VERSION

    print_info "Updating pyproject.toml version to $new_version"
    sed -i.bak "s/^version = \".*\"/version = \"$new_version\"/" pyproject.toml
    rm -f pyproject.toml.bak

    run_build_tests

    echo
    print_warning "This will:"
    echo "  - Update CHANGELOG.md ([Unreleased] -> [$new_version])"
    echo "  - Bump VERSION from $current_version to $new_version on $current_branch"
    echo "  - Commit and push $current_branch with the version bump"
    echo "  - Create git tag v$new_version"
    echo "  - Push the tag to origin (this triggers the GitHub Actions release build)"
    echo
    read -p "Proceed with release? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Release cancelled"
        exit 0
    fi

    print_info "Committing version bump to $current_branch"
    git add VERSION pyproject.toml uv.lock CHANGELOG.md
    git commit -m "chore: bump version to $new_version"

    print_info "Creating annotated tag v$new_version"
    git tag -a "v$new_version" -m "Release version $new_version"

    print_info "Pushing $current_branch"
    git push origin "$current_branch"

    print_info "Pushing tag v$new_version (this triggers GitHub Actions)"
    git push origin "v$new_version"

    print_success "Release $new_version created"
    print_info ""
    print_info "GitHub Actions will now build and publish:"
    print_info "  - styliteag/birdseye:$new_version"
    print_info "  - ghcr.io/styliteag/birdseye:$new_version"
    print_info ""
    local repo_slug
    repo_slug=$(git config --get remote.origin.url | sed 's/.*github.com[/:]//g' | sed 's/.git$//')
    print_info "Monitor: https://github.com/$repo_slug/actions"
}

main "$@"
