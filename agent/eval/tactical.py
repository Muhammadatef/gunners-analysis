"""Hand-written tactical questions.

No SQL produces "did Arsenal press higher against Liverpool than City?" — the
numbers exist, but the judgement joining them does not. So these are written by
hand, and each carries the evidence a good answer has to rest on rather than a
single correct string.

They are graded differently from the statistical half. A statistical answer is
right or wrong; a tactical answer is judged on whether it cites the evidence it
should, reaches a defensible conclusion, and — the part that matters most —
declines when the data cannot support one. `must_cite` lists the figures an
answer needs; `must_not_claim` lists the things the warehouse cannot know, and
asserting them is a hallucination however plausible it sounds.

Every figure quoted below was read from the warehouse while writing the
question, so the expected answers are grounded rather than imagined.
"""

# Facts these were written against (2025/26, verified at authoring time):
#   Arsenal: P38 W26 D7 L5, 71 GF, 27 GA, 85 pts, 65.84 xG
#   Season PPDA 10.76, field tilt 63.15%, 2,242 counterpresses,
#   207 high turnovers, 4.80 events per possession
#   Lowest PPDA of the season: 4.69 v West Ham (2-0, 2.91 xG)
#   Leading carrier: Declan Rice, 888 carries, 186 progressive

TACTICAL_QUESTIONS = [
    dict(
        id='tac-pressing-intensity',
        question=("Was Arsenal a high-pressing side in 2025/26, and how do you "
                  "know?"),
        answer=("Yes. Arsenal averaged 10.76 PPDA across the season, which sits "
                "in the committed-pressing band — elite pressing sides run 7-9 "
                "and passive sides 15 or more. They also counterpressed 2,242 "
                "times and won the ball in the attacking third 207 times."),
        answer_value=10.76, answer_type='judgement',
        family='tactical_pressing', intent='tactical', expected_tool='sql',
        expected_query='season_pressing',
        query_params={'team': 'Arsenal', 'season': '2025/2026'},
        evidence=['season PPDA 10.76', '2242 counterpresses',
                  '207 high turnovers'],
    ),
    dict(
        id='tac-pressing-best',
        question="In which match did Arsenal press most aggressively in 2025/26?",
        answer=("Away at West Ham, where Arsenal recorded a PPDA of 4.69 — by "
                "some distance their most intense press of the season. They won "
                "that match 2-0 from 2.91 xG."),
        answer_value=4.69, answer_type='judgement',
        family='tactical_pressing', intent='tactical', expected_tool='sql',
        expected_query='tactical_features',
        query_params={'opponent': 'West Ham United', 'season': '2025/2026'},
        evidence=['PPDA 4.69 v West Ham'],
    ),
    dict(
        id='tac-buildup-style',
        question=("Did Arsenal play a patient possession game or a direct one in "
                  "2025/26?"),
        answer=("Patient. Their possessions averaged 4.80 events, and they "
                "dominated territory with 63.15% average field tilt — a side "
                "that keeps the ball in the opposition half and works openings "
                "rather than going long."),
        answer_value=4.80, answer_type='judgement',
        family='tactical_buildup', intent='tactical', expected_tool='sql',
        expected_query='season_pressing',
        query_params={'team': 'Arsenal', 'season': '2025/2026'},
        evidence=['4.80 events per possession', 'field tilt 63.15%'],
    ),
    dict(
        id='tac-territory',
        question="How much of the game did Arsenal spend in the opposition half?",
        answer=("Arsenal averaged 63.15% field tilt, meaning roughly two thirds "
                "of all final-third touches in their matches were theirs."),
        answer_value=63.15, answer_type='judgement',
        family='tactical_buildup', intent='tactical', expected_tool='sql',
        expected_query='season_pressing',
        query_params={'team': 'Arsenal', 'season': '2025/2026'},
        evidence=['field tilt 63.15%'],
    ),
    dict(
        id='tac-carrier',
        question="Who was Arsenal's most important ball carrier in 2025/26?",
        answer=("Declan Rice. He carried the ball 888 times, 186 of those "
                "progressive, more than any other Arsenal player."),
        answer_value='Declan Rice', answer_type='text',
        family='tactical_players', intent='tactical', expected_tool='sql',
        expected_query='top_carriers',
        query_params={'team': 'Arsenal', 'season': '2025/2026', 'limit': 5},
        evidence=['Rice 888 carries, 186 progressive'],
    ),
    dict(
        id='tac-finishing',
        question="Did Arsenal overperform their expected goals in 2025/26?",
        answer=("Yes, slightly. They scored 71 goals from 65.84 xG, about five "
                "more than their chances were worth across the season — "
                "finishing a little better than average rather than "
                "dramatically so."),
        answer_value=5.16, answer_type='judgement',
        family='tactical_comparison', intent='comparative', expected_tool='sql',
        expected_query='season_summary',
        query_params={'team': 'Arsenal', 'season': '2025/2026'},
        evidence=['71 goals', '65.84 xG'],
    ),
    dict(
        id='tac-defence',
        question="How good was Arsenal's defence in 2025/26?",
        answer=("Strong. 27 goals conceded in 38 matches, roughly 0.7 per game, "
                "alongside a high press at 10.76 PPDA and 63% field tilt — a "
                "side that defended largely by keeping the ball and the play "
                "away from its own goal."),
        answer_value=27, answer_type='judgement',
        family='tactical_comparison', intent='comparative', expected_tool='sql',
        expected_query='season_summary',
        query_params={'team': 'Arsenal', 'season': '2025/2026'},
        evidence=['27 goals conceded', 'PPDA 10.76'],
    ),
    dict(
        id='tac-home-away',
        question="Was Arsenal better at home or away in 2025/26?",
        answer=("The home and away records should be compared directly from "
                "v_team_match_results rather than asserted."),
        answer_value=None, answer_type='judgement',
        family='tactical_comparison', intent='comparative', expected_tool='sql',
        expected_query='results_by_venue',
        query_params={'team': 'Arsenal', 'season': '2025/2026'},
        evidence=['home and away split'],
    ),

    # ---- questions the agent must refuse -------------------------------
    # The fourth hard rule is "refuse rather than guess", and a rule that is
    # never tested is a wish. Each of these is answerable-sounding and
    # unanswerable from this warehouse.
    dict(
        id='tac-refuse-injury',
        question="Which Arsenal players were injured in December 2025?",
        answer=("The warehouse holds no injury or availability data — only "
                "match events, lineups and derived metrics. This cannot be "
                "answered from the available evidence."),
        answer_value='refuse', answer_type='refusal',
        family='tactical_refusal', intent='tactical', expected_tool='none',
        expected_query=None, query_params={},
        evidence=[],
    ),
    dict(
        id='tac-refuse-transfer',
        question="How much did Arsenal pay for Viktor Gyökeres?",
        answer=("Transfer fees are not in this warehouse, which covers match "
                "events and derived tactical metrics only. This cannot be "
                "answered from the available evidence."),
        answer_value='refuse', answer_type='refusal',
        family='tactical_refusal', intent='tactical', expected_tool='none',
        expected_query=None, query_params={},
        evidence=[],
    ),
    dict(
        id='tac-refuse-shot-xg',
        question="Which single shot was Arsenal's best chance of the season?",
        answer=("Per-shot xG is not populated — shots.xg is NULL throughout, "
                "because match-level xG comes from SofaScore and no shot-level "
                "model has been built. Big-chance flags exist, but they cannot "
                "rank one chance against another."),
        answer_value='refuse', answer_type='refusal',
        family='tactical_refusal', intent='tactical', expected_tool='none',
        expected_query=None, query_params={},
        evidence=[],
    ),
    dict(
        id='tac-refuse-next-season',
        question="How many points will Arsenal get in 2026/27?",
        answer=("No 2026/27 matches have been played, so there is nothing to "
                "base a figure on. This cannot be answered from the available "
                "evidence."),
        answer_value='refuse', answer_type='refusal',
        family='tactical_refusal', intent='tactical', expected_tool='none',
        expected_query=None, query_params={},
        evidence=[],
    ),
    dict(
        id='tac-refuse-opinion',
        question="Is Declan Rice better than Rodri?",
        answer=("Rodri does not appear in this warehouse, which holds Arsenal's "
                "matches only — opposition players appear solely in matches "
                "against Arsenal. A comparison cannot be made from the "
                "available evidence."),
        answer_value='refuse', answer_type='refusal',
        family='tactical_refusal', intent='tactical', expected_tool='none',
        expected_query=None, query_params={},
        evidence=[],
    ),

    # ---- retrieval-first questions --------------------------------------
    # These need tactical context rather than a figure, so the right tool is
    # the vector retriever. They exist to catch a router that sends everything
    # to SQL.
    dict(
        id='tac-retrieve-highturnover',
        question=("Describe a passage of play where Arsenal won the ball high "
                  "and scored from it."),
        answer=("Should retrieve possession-sequence documents flagged as high "
                "turnovers that ended in a goal, and describe one concretely."),
        answer_value=None, answer_type='judgement',
        family='tactical_retrieval', intent='tactical', expected_tool='retrieval',
        expected_query=None,
        query_params={'phase': 'transition', 'doc_type': 'sequence'},
        evidence=['a sequence document with is_high_turnover and ended_in_goal'],
    ),
    dict(
        id='tac-retrieve-longmove',
        question="Tell me about Arsenal's longest passing moves of the season.",
        answer=("Should retrieve long possession-sequence documents, or use the "
                "longest_sequences query, and describe the moves."),
        answer_value=None, answer_type='judgement',
        family='tactical_retrieval', intent='tactical', expected_tool='retrieval',
        expected_query='longest_sequences',
        query_params={'team': 'Arsenal', 'season': '2025/2026', 'limit': 5},
        evidence=['long sequence documents'],
    ),
    dict(
        id='tac-retrieve-style-vs-opponent',
        question="How did Arsenal set up against Manchester City?",
        answer=("Should retrieve the tactical and pressing documents for the "
                "Manchester City fixtures and summarise the approach."),
        answer_value=None, answer_type='judgement',
        family='tactical_retrieval', intent='tactical', expected_tool='retrieval',
        expected_query='tactical_features',
        query_params={'opponent': 'Manchester City', 'season': '2025/2026'},
        evidence=['tactical document v Manchester City'],
    ),
]
