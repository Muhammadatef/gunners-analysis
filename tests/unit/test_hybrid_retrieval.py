"""Fusion and diversity — the parts of hybrid retrieval that need no database."""

from agent.tools import retriever


def _doc(content, match_id=1, doc_type='tactical', minute=None):
    return {'content': content, 'doc_type': doc_type, 'match_id': match_id,
            'minute': minute, 'opponent': 'Leeds', 'match_date': None,
            'phase': None, 'team_id': 1, 'score_state': None, 'score': 0.5}


def test_rrf_rewards_a_document_both_arms_agree_on():
    both = _doc('PPDA was 10.76 in the first half', minute=1)
    dense_only = _doc('Arsenal built from the back patiently', minute=2)
    sparse_only = _doc('Zone 14 entries came from the left', minute=3)

    fused = retriever.fuse([[dense_only, both], [sparse_only, both]])
    assert fused[0]['content'] == both['content']
    assert len(fused) == 3


def test_fusion_needs_no_common_score_scale():
    # Cosine ~0.4 against ts_rank_cd ~12: only the ranks are comparable.
    a, b = _doc('a', minute=1), _doc('b', minute=2)
    a['score'], b['score'] = 0.42, 12.0
    fused = retriever.fuse([[a], [b]])
    assert {row['content'] for row in fused} == {'a', 'b'}
    assert all(row['score'] < 1 for row in fused)


def test_diversify_drops_a_near_duplicate_in_favour_of_a_new_fact():
    rows = [
        _doc('Arsenal pressed high and forced a turnover on the left', minute=1),
        _doc('Arsenal pressed high and forced a turnover on the left wing',
             minute=2),
        _doc('Raya distributed long to beat the first line', minute=3),
    ]
    chosen = retriever.diversify(rows, k=2, lambda_=0.5)
    assert chosen[0] is rows[0]
    assert chosen[1] is rows[2]


def test_diversify_with_lambda_one_is_plain_truncation():
    rows = [_doc(str(i), minute=i) for i in range(5)]
    assert retriever.diversify(rows, k=3, lambda_=1.0) == rows[:3]


def test_diversify_handles_fewer_candidates_than_asked_for():
    rows = [_doc('only one')]
    assert len(retriever.diversify(rows, k=5)) == 1
    assert retriever.diversify([], k=5) == []


def test_lexical_terms_drop_question_words():
    terms = retriever._lexical_terms('How did Arsenal press against Man City?')
    assert 'how' not in terms.split(' or ')
    assert 'arsenal' in terms and 'press' in terms


def test_lexical_terms_are_ored_so_one_missing_word_is_not_fatal():
    # websearch_to_tsquery ANDs by default, which matches nothing on a
    # sentence-length query.
    assert ' or ' in retriever._lexical_terms('ppda in the Leeds match')


def test_lexical_terms_of_a_stopword_only_question_are_empty():
    assert retriever._lexical_terms('what about that?') == ''
