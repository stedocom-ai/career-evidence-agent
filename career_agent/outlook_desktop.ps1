param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('check', 'send')]
    [string]$Operation,
    [Parameter(Mandatory = $true)]
    [string]$AccountEmail,
    [string]$PayloadPath,
    [string]$AttachmentPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$outcome = 'failed'
$errorLabel = 'Classic Outlook could not be opened. Open Outlook and complete account setup first.'
$app = $null
$session = $null
$accounts = $null
$account = $null
$mail = $null
$result = $null
$exitCode = 0

try {
    $app = New-Object -ComObject Outlook.Application
    $session = $app.Session
    $accounts = $session.Accounts
    $errorLabel = 'The configured sender account was not found in classic Outlook. Add this account in Outlook first.'
    for ($index = 1; $index -le $accounts.Count; $index++) {
        $candidate = $accounts.Item($index)
        if ([string]::Equals([string]$candidate.SmtpAddress, $AccountEmail, [StringComparison]::OrdinalIgnoreCase)) {
            $account = $candidate
            break
        }
        if ([Runtime.InteropServices.Marshal]::IsComObject($candidate)) {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($candidate)
        }
    }
    if ($null -eq $account) { throw 'account_not_found' }

    if ($Operation -eq 'check') {
        $result = @{ status = 'ready'; mail = [string]$account.SmtpAddress; display_name = [string]$account.DisplayName; provider = 'outlook-desktop' }
    } else {
        $errorLabel = 'The draft or attachment could not be read. Create a fresh local draft and check its files.'
        if (-not (Test-Path -LiteralPath $PayloadPath -PathType Leaf)) { throw 'payload_missing' }
        if (-not (Test-Path -LiteralPath $AttachmentPath -PathType Leaf)) { throw 'attachment_missing' }
        $payload = Get-Content -LiteralPath $PayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not [string]::Equals([string]$payload.sender_email, $AccountEmail, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'sender_mismatch'
        }
        if ([string]$payload.recipient -notmatch '^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$') { throw 'invalid_recipient' }
        if ([string]::IsNullOrWhiteSpace([string]$payload.subject) -or [string]$payload.subject -match '[\r\n]') { throw 'invalid_subject' }
        if ([string]::IsNullOrWhiteSpace([string]$payload.body)) { throw 'empty_body' }

        $errorLabel = 'Classic Outlook could not prepare the message. Check its account profile and attachment.'
        $mail = $app.CreateItem(0)
        $mail.SendUsingAccount = $account
        $mail.BodyFormat = 1
        $mail.To = [string]$payload.recipient
        $mail.Subject = [string]$payload.subject
        $mail.Body = [string]$payload.body
        $attachment = $mail.Attachments.Add($AttachmentPath, 1)
        if ([Runtime.InteropServices.Marshal]::IsComObject($attachment)) {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($attachment)
        }
        $accessor = $mail.PropertyAccessor
        $accessor.SetProperty('http://schemas.microsoft.com/mapi/string/{00020386-0000-0000-C000-000000000046}/X-Career-Agent-Id', [string]$payload.correlation_id)
        if ([Runtime.InteropServices.Marshal]::IsComObject($accessor)) {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($accessor)
        }
        $errorLabel = 'Outlook send result is uncertain. Check Outbox and Sent Items before retrying.'
        $outcome = 'unknown'
        $mail.Send()
        $result = @{ status = 'accepted'; provider_reference = 'classic-outlook-queued' }
    }
} catch {
    $result = @{ status = 'error'; outcome = $outcome; error = $errorLabel }
    $exitCode = 1
} finally {
    foreach ($object in @($mail, $account, $accounts, $session, $app)) {
        if ($null -ne $object -and [Runtime.InteropServices.Marshal]::IsComObject($object)) {
            try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($object) } catch { }
        }
    }
}

$result | ConvertTo-Json -Depth 5 -Compress
exit $exitCode
