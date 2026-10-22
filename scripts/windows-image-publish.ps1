# Builds the Windows container image of the bootstrapper (Dockerfile.windows) on a Windows runner and pushes it to Nexus.
# Used by the bootstrapper_windows_image_* jobs of .gitlab-ci.yml; needs NEXUS_REPO_PASSWORD (and CI_REGISTRY_PASSWORD +
# DOCKER_REGISTRY for the Docker Hub copy of the release tags).
#
# Tags pushed to nexus.hq.molo17.com/molo17-local/gluesync-bootstrapper:
#   tag-vX.Y.Z[-alpha.N] / beta-vX.Y.Z   X.Y.Z[-...]-win-nanoserver-<ltsc>    (the version is derived like the Linux deploy job:
#                                         the part of the tag that starts at the first N.N.N)
#   tag-vX.Y.Z (a GA release)            also latest-win-nanoserver-<ltsc>    (alpha and beta tags leave latest alone,
#                                         like fix/ci-latest-only-on-release does for the Linux image)
#   branch or merge request pipeline     <branch-slug>-win-nanoserver-<ltsc>   (so a demo-kit can use the image before the merge)
# Writes bootstrapper-windows-image.env (dotenv artifact: IMAGE_REF, IMAGE_VERSION, IMAGE_TAGS) for the smoke job.
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet('ltsc2019', 'ltsc2022', 'ltsc2025')][string]$WindowsTag
)
$ErrorActionPreference = 'Stop'

function Assert-Exit {
  param([string]$Message)
  if ($LASTEXITCODE -ne 0) { throw "$Message (exit code $LASTEXITCODE)" }
}

function ConvertTo-Slug {
  # the GitLab CI_COMMIT_REF_SLUG rule: lowercase, anything but a-z 0-9 becomes '-', no leading/trailing '-', 63 characters
  param([string]$Name)
  $slug = ($Name.ToLowerInvariant() -replace '[^a-z0-9]', '-')
  if ($slug.Length -gt 63) { $slug = $slug.Substring(0, 63) }
  return $slug.Trim('-')
}

$nexusRegistry = 'nexus.hq.molo17.com'
$app = if ($env:APP_NAME) { $env:APP_NAME } else { 'gluesync-bootstrapper' }
$nexusName = "$nexusRegistry/molo17-local/$app"
$suffix = "win-nanoserver-$WindowsTag"

if ($env:CI_COMMIT_TAG) {
  $version = [regex]::Match($env:CI_COMMIT_TAG, '[0-9]+\.[0-9]+\.[0-9](.*)').Value
  if (-not $version) { throw "Could not extract a version from the tag $($env:CI_COMMIT_TAG)" }
  $tags = @("$version-$suffix")
  if ($env:CI_COMMIT_TAG -match '^tag-v?[0-9]+\.[0-9]+\.[0-9]+$') { $tags += "latest-$suffix" }
  $release = $true
}
else {
  $branch = if ($env:CI_MERGE_REQUEST_SOURCE_BRANCH_NAME) { $env:CI_MERGE_REQUEST_SOURCE_BRANCH_NAME }
            elseif ($env:CI_COMMIT_BRANCH) { $env:CI_COMMIT_BRANCH }
            else { $env:CI_COMMIT_REF_NAME }
  $slug = ConvertTo-Slug $branch
  if (-not $slug) { throw "Could not derive a branch slug from '$branch'" }
  $version = "$slug-$($env:CI_COMMIT_SHORT_SHA)"
  $tags = @("$slug-$suffix")
  $release = $false
}
$primary = "${nexusName}:$($tags[0])"
Write-Host "Windows image $WindowsTag, version $version, tags: $($tags -join ', ')"

Write-Host 'Logging in to Nexus...'
$env:NEXUS_REPO_PASSWORD | docker login -u gluesync --password-stdin $nexusRegistry
Assert-Exit 'Docker login to Nexus failed'

Set-Content -Path 'VERSION' -Value $version -Encoding ascii
Write-Host "VERSION file: $(Get-Content VERSION)"

docker build --file Dockerfile.windows --build-arg "WINDOWS_TAG=$WindowsTag" --build-arg "VERSION=$version" --tag $primary .
Assert-Exit 'Docker build failed'

foreach ($tag in $tags) {
  $ref = "${nexusName}:$tag"
  if ($ref -ne $primary) {
    docker tag $primary $ref
    Assert-Exit "Docker tag $ref failed"
  }
  docker push $ref
  Assert-Exit "Docker push $ref failed"
  Write-Host "Pushed $ref"
}

$size = (docker image inspect $primary --format '{{.Size}}')
Assert-Exit 'Docker image inspect failed'
Write-Host ("Image size: {0:N1} MB ({1})" -f ([double]$size / 1MB), $primary)

if ($release -and $env:DOCKER_REGISTRY -and $env:CI_REGISTRY_PASSWORD) {
  # the Linux deploy job also publishes to Docker Hub (molo17/gluesync-bootstrapper)
  Write-Host 'Logging in to Docker Hub...'
  $env:CI_REGISTRY_PASSWORD | docker login -u danieleangeli --password-stdin
  Assert-Exit 'Docker login to Docker Hub failed'
  foreach ($tag in $tags) {
    $hubRef = "$($env:DOCKER_REGISTRY)/${app}:$tag"
    docker tag $primary $hubRef
    Assert-Exit "Docker tag $hubRef failed"
    docker push $hubRef
    Assert-Exit "Docker push $hubRef failed"
    Write-Host "Pushed $hubRef"
  }
}

@(
  "IMAGE_REF=$primary",
  "IMAGE_VERSION=$version",
  "IMAGE_TAGS=$($tags -join ',')",
  "IMAGE_SIZE_MB=$([math]::Round([double]$size / 1MB, 1))"
) | Set-Content -Path "bootstrapper-windows-image.env" -Encoding ascii
Write-Host "Pushed tags: $(($tags | ForEach-Object { "${nexusName}:$_" }) -join ', ')"
