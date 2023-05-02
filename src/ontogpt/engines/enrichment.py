"""Enrichment engine."""
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from jinja2 import Template
from oaklib import BasicOntologyInterface, get_adapter

from ontogpt.engines.knowledge_engine import KnowledgeEngine
from ontogpt.prompts.enrichment import DEFAULT_ENRICHMENT_PROMPT
from ontogpt.templates.gene_description_term import GeneDescriptionTerm
from ontogpt.utils.gene_set_utils import (
    ENTITY_ID,
    GENE_TUPLE,
    EnrichmentPayload,
    GeneSet,
    gene_info,
)

logger = logging.getLogger(__name__)


SUMMARY_KEYWORD = "Summary"
MECHANISM_KEYWORD = "Mechanism"
ENRICHED_TERMS_KEYWORD = "Enriched Terms"


class GeneDescriptionSource(Enum):
    NONE = "none"
    ONTOLOGICAL_SYNOPSIS = "ontological"
    NARRATIVE_SYNOPSIS = "narrative"
    COMBINED_SYNOPSIS = "combined"
    WIKIPEDIA = "wikipedia"
    PUBMED = "pubmed"


@dataclass
class EnrichmentEngine(KnowledgeEngine):
    """Engine for performing term enrichment.

    Algorithm: SPINDOCTOR

    Summarizing Proteins Interpolating Narrative
    Descriptions of Controlled Terms for Ontological enRichment

    """

    # engine: str = "text-davinci-003"
    # model: str = "xxxgpt-3.5-turbo"

    label_resolvers: Dict[str, BasicOntologyInterface] = field(default_factory=dict)

    completion_length = 250

    prompt_template: str = None
    """Path to a jinja2 template for the prompt"""

    def __post_init__(self):
        if not self.template:
            self.template = "gene_description_term"
        super().__post_init__()

    def __key(self):
        return "enrichment_engine"

    def __eq__(self, other):
        if isinstance(other, EnrichmentEngine):
            return self.__key() == other.__key()
        return NotImplemented

    def __hash__(self):
        return hash(self.__key())

    def summarize(
        self,
        gene_set: GeneSet,
        prompt_template: Optional[str] = None,
        normalize=False,
        strict=False,
        gene_description_source: GeneDescriptionSource = None,
        ontological_synopsis=True,
        combined_synopsis=False,
        annotations=True,
        gene_aliases=None,
    ) -> EnrichmentPayload:
        """
        Summarize a list of gene IDs.

        >>> engine = EnrichmentEngine()
        >>> gene_set = GeneSet(gene_ids=...)
        >>> engine.summarize(gene_set)

        :param gene_set: an object representing a list of genes to summarize
        :prompt_template: path to alternative prompt
        :param normalize: whether to normalize labels to IDs
        :param strict: whether to raise an error if a gene symbol cannot be normalized
        :param ontological_synopsis: whether to use the auto-generated synopsis
        :param combined_synopsis: whether to combine the auto-generated and manual synopsis
        :param annotations: whether to use annotations
        :param spike_gene_set: FOR TESTING ONLY
        :return: summary
        """
        if gene_description_source:
            if gene_description_source == GeneDescriptionSource.NONE:
                ontological_synopsis = False
                annotations = False
            elif gene_description_source == GeneDescriptionSource.ONTOLOGICAL_SYNOPSIS:
                ontological_synopsis = True
                annotations = True
            elif gene_description_source == GeneDescriptionSource.NARRATIVE_SYNOPSIS:
                ontological_synopsis = False
                annotations = True
            elif gene_description_source == GeneDescriptionSource.COMBINED_SYNOPSIS:
                combined_synopsis = True
            else:
                raise NotImplementedError
        if not gene_set.gene_ids and not gene_set.gene_symbols:
            raise ValueError(f"Gene set {gene_set.name} has no gene symbols or ids")
        # if gene_set.gene_ids and not gene_set.gene_symbols:
        #    adapter = list(self.label_resolvers.values())[0]
        #    gene_set.gene_symbols = [adapter.label(x.lower()) for x in gene_set.gene_ids]
        if not gene_set.gene_ids or normalize:
            gene_set.gene_ids = list(self.map_labels(gene_set.gene_symbols, strict=strict))
            logger.info(f"gene ids: {gene_set.gene_ids}")
        gene_tuples = []
        for gene_id in gene_set.gene_ids:
            logging.info(f"Looking up gene summary for {gene_id}...")
            symbol, desc_manual, desc_auto = gene_info(gene_id)
            if combined_synopsis:
                if ontological_synopsis:
                    desc = desc_auto + "; " + desc_manual
                    logging.debug(f"Combined synopsis (priority: auto): {desc}")
                else:
                    desc = desc_manual + "; " + desc_auto
                    logging.debug(f"Combined synopsis (priority: manual): {desc}")
            elif ontological_synopsis:
                desc = desc_auto
                logging.debug(f"Auto synopsis: {desc}")
            else:
                desc = desc_manual
                logging.debug(f"Manual synopsis: {desc}")
            gene_tuples.append((gene_id, symbol, desc))
        logging.info(f"Found {len(gene_tuples)} gene summaries")
        # if not annotations:
        #    return self.summarize_annotation_free(gene_tuples)
        if gene_aliases:
            gene_tuples = [(id, gene_aliases.get(sym, sym), desc) for id, sym, desc in gene_tuples]
        if not prompt_template:
            prompt_template = str(f"{DEFAULT_ENRICHMENT_PROMPT}.jinja2")
        prompt, tf = self._prompt_from_template(
            gene_tuples,
            template=prompt_template,
            annotations=annotations,
            taxon=gene_set.taxon,
        )
        response_text = self.client.complete(prompt, max_tokens=self.completion_length)
        response_token_length = len(self.encoding.encode(response_text))
        logging.info(f"Response token length: {response_token_length}")
        payload = EnrichmentPayload(
            prompt=prompt,
            response_text=response_text,
            truncation_factor=tf,
            used_auto_synopsis=ontological_synopsis,
            used_combined_synopsis=combined_synopsis,
            annotations=annotations,
            response_token_length=response_token_length,
            model=self.model,
        )
        self.process_payload(payload)
        return payload

    def _prompt_from_template(
        self,
        genes: List[GENE_TUPLE],
        template: Union[str, Path, Template],
        truncation_factor=1.0,
        taxon: str = None,
        annotations=True,
    ) -> Tuple[str, float]:
        if isinstance(template, Path):
            template = str(template)
        if isinstance(template, str):
            # create a Jinja2 template object
            with open(template) as file:
                template = Template(file.read())
        if not isinstance(template, Template):
            raise ValueError(f"Invalid template: {template}")
        gd_tuples = []
        for _, symbol, desc in genes:
            if desc is None:
                desc = ""
            if truncation_factor < 1.0:
                desc = desc[: int(len(desc) * truncation_factor)] + "..."
            gd_tuples.append((symbol, desc))
        prompt = template.render(
            gene_descriptions=gd_tuples,
            annotations=annotations,
            taxon=taxon,
            SUMMARY_KEYWORD=SUMMARY_KEYWORD,
            MECHANISM_KEYWORD=MECHANISM_KEYWORD,
            ENRICHED_TERMS_KEYWORD=ENRICHED_TERMS_KEYWORD,
        )
        logging.debug(f"Prompt from template: {prompt}")
        logging.info(f"Prompt [{truncation_factor}] Length: {len(prompt)}")
        # https://help.openai.com/en/articles/4936856-what-are-tokens-and-how-to-count-them
        prompt_length = len(self.encoding.encode(prompt)) + 10
        max_len = 4097 - self.completion_length
        logging.info(
            f"Prompt [{truncation_factor}] Toks: {prompt_length} / {max_len} Str={len(prompt)}"
        )
        if prompt_length > max_len:  # TODO: check this
            logging.warning(f"Prompt is too long; toks: {prompt_length} len: {len(prompt)}")
            return self._prompt_from_template(
                genes,
                template,
                truncation_factor=truncation_factor * 0.8,
                taxon=taxon,
                annotations=annotations,
            )
        return prompt, truncation_factor

    def map_labels(self, labels: List[str], strict=False) -> Iterator[ENTITY_ID]:
        """Map labels to IDs."""
        for label in labels:
            if ":" in label and " " not in label:
                yield label
            for adapter in self.label_resolvers.values():
                try:
                    curies = [x.upper() for x in adapter.curies_by_label(label)]
                    if len(curies) != 1:
                        logging.warning(f"Found {len(curies)} curies for label: {label}")
                        if strict:
                            raise ValueError(f"Found {len(curies)} curies for label: {label}")
                    if curies:
                        yield curies[0]
                except ValueError:
                    continue

    def add_resolver(self, resolver: str):
        self.label_resolvers[resolver] = get_adapter(resolver)

    def process_payload(self, payload: EnrichmentPayload) -> EnrichmentPayload:
        """Process the payload."""
        toks = re.split(f"{ENRICHED_TERMS_KEYWORD}:", payload.response_text, flags=re.IGNORECASE)
        if len(toks) > 2:
            logging.warning(
                f"Found more than one {ENRICHED_TERMS_KEYWORD} in response: {payload.response_text}"
            )
            toks[1] = " ".join(toks[1:])
        if len(toks) > 1:
            payload.summary = toks[0]
            rest = toks[1]
        else:
            payload.summary = "COULD NOT PARSE"
            rest = payload.response_text
        if "hypothesis:" in rest.lower():
            # this is a little ad-hoc; turbo is prone to inserting a "hypothesis" at the end
            # even though this is not in the prompt
            toks = re.split("hypothesis:", rest, flags=re.IGNORECASE)
            rest = toks[0]
            payload.summary += "\nHypothesis: " + toks[1]
        # we ask to split on ";" but sometimes the model disobeys and uses "," instead
        tok_chars = [";", ",", "-"]
        tokenizations = {}
        for tok_char in tok_chars:
            toks = rest.split(f"{tok_char} ")
            tokenizations[tok_char] = toks
        tokenizations = sorted(tokenizations.items(), key=lambda x: len(x[1]), reverse=True)
        best_tokens = tokenizations[0][1]
        payload.term_strings = [s.lower().strip(":-*;. -\n") for s in best_tokens]  # noqa
        if payload.term_strings and payload.term_strings[-1].startswith("and "):
            # sometimes the LLM will write an oxford comma style list
            payload.term_strings[-1] = payload.term_strings[-1].replace("and ", "")
        payload.term_ids = []
        for term in payload.term_strings:
            payload.term_ids.append(self.normalize_named_entity(term, GeneDescriptionTerm.__name__))
        return payload
