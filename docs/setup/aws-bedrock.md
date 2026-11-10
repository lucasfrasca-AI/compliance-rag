# Setting up AWS for generation (Bedrock, Australian inference profile)

Generation runs on **Claude Sonnet 5 through Amazon Bedrock's Australian geographic inference
profile**, `au.anthropic.claude-sonnet-5`, called from `ap-southeast-2` (Sydney). The profile's
only destination Regions are Sydney and Melbourne (`ap-southeast-4`), so the model processes every
request in Australia (ADR-0056, #322). These are the steps only you can do: they need your AWS
account, a console, and a payment method. Claude Code is not given AWS access, and no step creates
an access key.

What you end up with:

| Where | Credential | How |
|---|---|---|
| Cloud Run (the API) | none stored | the runtime service account's Google ID token is exchanged at STS for the role `compliance-rag-bedrock` (`AWS_ROLE_ARN`) |
| Your laptop | none stored | a named IAM Identity Center (SSO) profile (`AWS_PROFILE`) |

Placeholders in this file, and only these: `<AWS_ACCOUNT_ID>` (12 digits) and `<SA_UNIQUE_ID>` (21
digits, step 6).

> **Before the first real call:** `SERVICES.md` still lists Bedrock under "wrong cloud for this
> build". ADR-0056 reverses that; make the matching `SERVICES.md` row yourself (service, region,
> estimated $/month, phase) so the billable protocol has something to point at. Until then the
> billable guard denies the `aws` commands that create or change things (below) and says why.

## 1. Sign in and pick the Region

Use the AWS console as an administrator of the account, and switch the Region selector to
**Asia Pacific (Sydney) `ap-southeast-2`** for everything below, except IAM (global).

## 2. Anthropic's first-time-use form

Anthropic models on Bedrock ask each AWS account for use-case details once.

1. Console, Amazon Bedrock, **Model catalog** (Sydney), pick **Anthropic, Claude Sonnet 5**.
2. If it shows a use-case form, complete it (company name, website, intended users, use case: a
   compliance-checking tool for corporates) and submit. Approval is normally immediate.

The same call exists on the CLI as `aws bedrock put-use-case-for-model-access`. It is a *write*, so
the billable guard denies it for Claude Code; run it yourself if you prefer it to the console.

Until the form is done, requests fail with a 404/403 saying use-case details have not been
submitted. The API shows that to the member as **unavailable, reason `access`**, not as an internal
error.

## 3. AWS Marketplace permissions and a payment method

Anthropic's models are sold through AWS Marketplace. The first invocation from an identity that is
allowed to subscribe creates the subscription.

1. **Payment method.** Billing console, Payment preferences: a valid card or invoice arrangement
   must be on the account. Without one, requests fail with `INVALID_PAYMENT_INSTRUMENT` (also
   shown as `access`).
2. **Subscribe once, as an administrator, not as the runtime role.** The identity you use for the
   first call needs `aws-marketplace:ViewSubscriptions` and `aws-marketplace:Subscribe` (the AWS
   managed policy `AmazonBedrockFullAccess` carries both, as does an administrator). Open
   **Playgrounds, Chat**, choose Claude Sonnet 5, and send one short message. Bedrock
   subscribes the account at that point; it can take a couple of minutes to settle.
3. The runtime role (step 7) and your local SSO role do **not** get Marketplace permissions. They
   only invoke.

## 4. Check the Service Quotas value

A new account's quota can be low, or zero, for a new model. Check before the first real load.

Console: Service Quotas, **AWS services, Amazon Bedrock** (Sydney), and read the value of
**"Cross-region model inference tokens per minute for Anthropic Claude Sonnet 5"** (and the
matching requests-per-minute quota). Read-only on the CLI:

```
aws service-quotas list-service-quotas --service-code bedrock --region ap-southeast-2 \
  --query "Quotas[?contains(QuotaName, 'Sonnet 5')].[QuotaName,Value,QuotaCode]" --output table
```

If it is too low for the evaluation or demo, request an increase in the console (Service Quotas,
the quota, **Request increase at account level**). That is a write; Claude Code cannot do it.
While the quota is zero or exhausted the API shows **unavailable, reason `quota`**.

## 5. Check that the profile stays in Australia

This is read-only and the check that matters most:

```
aws bedrock get-inference-profile --inference-profile-identifier au.anthropic.claude-sonnet-5 \
  --region ap-southeast-2
```

Confirm in the output that `status` is `ACTIVE` and that `models[].modelArn` lists **exactly two
foundation models, one in `ap-southeast-2` (Sydney) and one in `ap-southeast-4` (Melbourne)**,
nothing else. If any other Region appears, stop: do not continue and do not set
`BEDROCK_INFERENCE_PROFILE`. Copy the two `modelArn` values; step 7's policy must name exactly
those (the placeholders below assume `anthropic.claude-sonnet-5`; use whatever the output shows).

## 6. Find the service account's unique id (Google side, read-only)

The role's trust policy pins the **unique id** of the service account the API runs as, not its
email. The runtime service account is `compliance-rag-runtime` (`SETUP.md` §2); if the API runs as
another account, use that one's email.

```
gcloud iam service-accounts describe \
  compliance-rag-runtime@compliance-rag-508904.iam.gserviceaccount.com \
  --format='value(uniqueId)'
```

It prints about 21 digits. That is `<SA_UNIQUE_ID>`. It is an identifier, not a secret.

## 7. Create the role (web-identity federation, no key)

`accounts.google.com` is one of the identity providers IAM knows without being set up, so no OIDC
provider needs creating. What the trust policy checks:

- the token was issued by Google (`Principal: Federated: accounts.google.com`);
- `accounts.google.com:aud` is `<SA_UNIQUE_ID>`. For a service account's ID token, Google puts the
  account's unique id in the `azp` claim, and IAM maps `azp` to `aud`;
- `accounts.google.com:oaud` is `sts.amazonaws.com`: the token's own `aud` claim, which is the
  audience the API asks Google for (`AWS_WEB_IDENTITY_AUDIENCE`, default `sts.amazonaws.com`).
  Whoever can mint an ID token for another audience cannot use it here.

`trust-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Federated": "accounts.google.com" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "accounts.google.com:aud": "<SA_UNIQUE_ID>",
          "accounts.google.com:oaud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

`invoke-policy.json`, least privilege: invoke through the Australian profile, and the foundation
model in each of its two destination Regions only when it is reached through that profile.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeThroughTheAustralianProfile",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "arn:aws:bedrock:ap-southeast-2:<AWS_ACCOUNT_ID>:inference-profile/au.anthropic.claude-sonnet-5"
    },
    {
      "Sid": "TheFoundationModelInSydneyAndMelbourneOnlyViaThatProfile",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-sonnet-5",
        "arn:aws:bedrock:ap-southeast-4::foundation-model/anthropic.claude-sonnet-5"
      ],
      "Condition": {
        "StringLike": {
          "bedrock:InferenceProfileArn": "arn:aws:bedrock:ap-southeast-2:<AWS_ACCOUNT_ID>:inference-profile/au.anthropic.claude-sonnet-5"
        }
      }
    }
  ]
}
```

Create them (or do the same in the IAM console: Roles, Create role, **Web identity**, provider
**Google**, then edit the trust policy to the JSON above). The CLI writes are yours to run:

```
aws iam create-role --role-name compliance-rag-bedrock \
  --assume-role-policy-document file://trust-policy.json --max-session-duration 3600
aws iam put-role-policy --role-name compliance-rag-bedrock \
  --policy-name invoke-sonnet-5-au --policy-document file://invoke-policy.json
```

The role ARN is `arn:aws:iam::<AWS_ACCOUNT_ID>:role/compliance-rag-bedrock`. Set it as
`AWS_ROLE_ARN` on the Cloud Run service (Claude Code's billable protocol applies to that
`gcloud run services update`; it is not a key and not a secret). The session lasts an hour; the
API renews it five minutes before it lapses.

## 8. Your laptop: an SSO profile

Give your IAM Identity Center user a permission set carrying the same `invoke-policy.json` (inline
policy), assigned to the account. Then:

```
aws configure sso --profile compliance-rag      # sso region as set up; account; the permission set
aws sso login --profile compliance-rag
aws sts get-caller-identity --profile compliance-rag
```

Put `AWS_PROFILE=compliance-rag` in your local `.env` (and leave `AWS_ROLE_ARN` empty: setting
both is refused). When the login lapses, requests show as **unavailable, reason `access`**; run
`aws sso login --profile compliance-rag` again.

Never create an IAM user access key, and never set a Bedrock API key
(`AWS_BEARER_TOKEN_BEDROCK`): the application refuses to start with one, and no code reads the
long-lived access-key environment variables.

## 9. Local and Cloud Run settings

| Setting | Value | Where |
|---|---|---|
| `GENERATION_BACKEND` | `bedrock` (the default) | both |
| `BEDROCK_REGION` | `ap-southeast-2` (the only value accepted) | both |
| `BEDROCK_INFERENCE_PROFILE` | `au.anthropic.claude-sonnet-5` (must start with `au.`) | both |
| `AWS_ROLE_ARN` | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/compliance-rag-bedrock` | Cloud Run |
| `AWS_PROFILE` | `compliance-rag` | local |
| `AWS_WEB_IDENTITY_AUDIENCE` | `sts.amazonaws.com` (the default; must match `oaud` above) | Cloud Run |

`global.`, `us.` and `apac.` profiles, and any other source Region, are refused at startup: they
can route a request outside Australia.

## If something fails

The member sees "the model isn't taking requests right now" in every case below; the span on the
request (`generator.generate`) and the server log say which.

| Span / log | Meaning | Fix |
|---|---|---|
| `generator.unavailable=quota`, `provider_error_type=ThrottlingException` or `ServiceQuotaExceededException` | The Service Quotas value in step 4 is too low, or was exceeded | Request an increase, or wait |
| `generator.unavailable=access`, `provider_error_type=AccessDeniedException` | The role's policy doesn't name the profile or a destination model; or the model isn't enabled | Check step 7's ARNs against step 5's output; complete steps 2 and 3 |
| `access`, status 404, `ResourceNotFoundException` | The first-time-use form (step 2) isn't done | Complete it |
| `access`, message mentions a payment instrument or Marketplace | Step 3 isn't done | Add a payment method; subscribe once |
| `access`, `generator.credentials_error` = `STS refused the web-identity exchange (InvalidIdentityToken / AccessDenied)` | The trust policy doesn't match the token: the wrong `<SA_UNIQUE_ID>`, a different audience, or the API is running as another service account | Re-check step 6 and the `Principal`; see "Not verified" below |
| `access`, `credentials_error` names `aws sso login` | The local SSO session lapsed | `aws sso login --profile compliance-rag` |
| `internal`, `credentials_error` absent, "metadata server" in the log | The process isn't on Cloud Run, or the metadata server was unreachable | Run locally with `AWS_PROFILE`, not `AWS_ROLE_ARN` |

## Not verified from here (check on the first live call, #29/#31)

This repository has no AWS access, so these were written from AWS's documentation and are the
first things to check if the exchange is refused:

- That IAM exposes the token's `azp` claim as `accounts.google.com:aud` and its `aud` claim as
  `accounts.google.com:oaud` for a Google service-account ID token. If STS answers `AccessDenied`
  with a correct `<SA_UNIQUE_ID>`, drop the `oaud` line from the trust policy and keep `aud`, then
  tell us so the audience pin can be revisited.
- That a Cloud Run service account's ID token carries the unique id as `azp` (the default token
  format from the metadata server). Decode the token's payload (not its signature) to see the
  claims: `azp` and `sub` should both be `<SA_UNIQUE_ID>`.
- The exact `modelArn`s in step 5 (the foundation-model id may carry a version suffix) and the
  quota names in step 4.
