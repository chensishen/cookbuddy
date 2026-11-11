"""Shared display contract for verified ingredient comparisons and conditions."""
from answer_blocks import build_display_blocks


def inventory_sections(sections,references,audit_notes):
    # Inputs come from deterministic source-checked comparisons, not an LLM
    # rewrite of the final answer. Every visible section retains source anchors.
    payload={'claims':[{'kind':'answer','text':text,'evidence':evidence}
                       for heading,text,evidence in sections],'missing_information':''}
    blocks=build_display_blocks(payload,references)
    for block,(heading,_,_) in zip(blocks,sections):block['heading']=heading
    return {'short_answer':'\n\n'.join('### '+heading+'\n'+text for heading,text,_ in sections),
            'display_blocks':blocks,'answer_layout':'inventory_check','audit_notes':audit_notes}
