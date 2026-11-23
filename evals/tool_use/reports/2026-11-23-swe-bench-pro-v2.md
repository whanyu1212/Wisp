# SWE-bench Pro V2 pilot

On November 23, 2026, Wisp resolved all eight selected tasks under both prompt versions on
GPT-6.2 Sol (high effort) and GPT-6.2 Astra (medium effort). All **32 attempts passed** the official
verifiers. The sample covers five repositories and includes two Hard-51 tasks.

## Setup

- Wisp ran from [`7cdc25d`](https://github.com/whanyu1212/Wisp/commit/7cdc25dcc171f048526b7fcc3faa96ac9fd26016).
- The task images and verifiers came from the [pinned Pro V2 release](https://github.com/scaleapi/SWE-bench_Pro-os/tree/66f92766bba642462d4bbe5479e83f91f9211862/v2).
- Each model ran every task once per prompt. The two arms used the saved
  [before](../guidance/2026-11-23-verification-rounds-before.txt) and
  [after](../guidance/2026-11-23-verification-rounds-after.txt) core prompts. The Wisp build, tool set,
  task image, model setting, and time caps matched within each pair.
- The solver could reach only the model endpoint and had 20 minutes to produce a patch. Each
  patch was graded in a fresh container with the official verifier and a separate 12-minute cap.
  On every selected task, the reference patch passed and the empty patch failed.

## Results

| Model setting | Passes before | Passes after | Tool rounds per pass before | Tool rounds per pass after |
|---|---:|---:|---:|---:|
| GPT-6.2 Sol high | 8/8 | 8/8 | 35.00 | 29.62 |
| GPT-6.2 Astra medium | 8/8 | 8/8 | 16.25 | 17.88 |

All attempts in the table below passed. A tool round is one model response that requests one or
more tool calls.

| Task | Hard-51 | Sol before | Sol after | Astra before | Astra after |
|---|---|---:|---:|---:|---:|
| [qutebrowser startup error message](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_qutebrowser__qutebrowser-0b621cb0ce2b54d3f93d8d41d8ff4257888a87e5-v2ef375ac784985212b1805e1d0431dc8f1b3c171/instruction.md) | No | 26 | 19 | 13 | 13 |
| [qutebrowser key sequence types](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_qutebrowser__qutebrowser-3e21c8214a998cb1058defd15aabb24617a76402-v5fc38aaf22415ab0b70567368332beee7955b367/instruction.md) | Yes | 29 | 25 | 11 | 14 |
| [Open Library MARC author roles](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_internetarchive__openlibrary-08ac40d050a64e1d2646ece4959af0c42bf6b7b5-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4/instruction.md) | No | 49 | 36 | 17 | 18 |
| [Open Library Markdown table of contents](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_internetarchive__openlibrary-09865f5fb549694d969f0a8e49b9d204ef1853ca-ve8c8d62a2b60610a3c4631f5f23ed866bada9818/instruction.md) | No | 18 | 16 | 8 | 8 |
| [Open Library alternate-script author names](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_internetarchive__openlibrary-0dc5b20fa186f9714f8a838178597e69f549d026-v2d9a6c849c60ed19fd0858ce9e40b7cc8e097e59/instruction.md) | Yes | 27 | 29 | 14 | 15 |
| [NodeBB topic sort order](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_NodeBB__NodeBB-05f2236193f407cf8e2072757fbd6bb170bc13f0-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e/instruction.md) | No | 21 | 17 | 15 | 17 |
| [Navidrome album grid](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_navidrome__navidrome-0488fb92cb02a82924fb1181bf1642f2e87096db/instruction.md) | No | 58 | 45 | 24 | 27 |
| [Flipt filesystem namespace versions](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tasks/instance_flipt-io__flipt-05d7234fa582df632f70a7cd10194d61bd7043b9/instruction.md) | No | 52 | 50 | 28 | 31 |
| **Total tool rounds** | | **280** | **237** | **130** | **143** |

The Sol high run on Navidrome with the before prompt reached the solve deadline. The patch it
had produced by the deadline passed the verifier.

This was a selected eight-task sample from the 642-task public suite. The observed changes in tool
rounds are preliminary because each model and prompt combination ran once per task.
