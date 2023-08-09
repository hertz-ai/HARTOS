from langchain.llms.base import LLM


class CustomGPT(LLM):
    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        response = requests.post(
            "http://aws_rasa.hertzai.com:5459/gpt-4",
            json={
              "model": "gpt-3.5-turbo-16k",
              "data": [{"role":"user","content":prompt}]
            }
        )
        response.raise_for_status()
        return response.json()["text"]

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }


def get_action_user_details(user_id):


    action_url = f"http://aws_hevolve.hertzai.com:6006/action_by_user_id?user_id={user_id}"

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    unwanted_actions=['Casual Conversation', 'Topic confirmation', 'Topic not found', 'Topic confirmation', 'Topic listing', 'Probe', 'Question Answering', 'Fallback']
    data = response.json()
    action_texts = [obj["action"] for obj in data if obj["action"] not in unwanted_actions]
    if len(action_texts)==0:
        action_texts=['user has not performed any actions yet']
    actions = ", ".join(action_texts)


    # user detail api

    url = "http://aws_hevolve.hertzai.com:6006/getstudent_by_user_id"
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    # print()

    user_data = response.json()

    user_details = f'''Below are the information about the user.
    user_name: {user_data["name"]} (Call the user by this name),gender: {user_data["gender"]},who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees),preferred_language: {user_data["preferred_language"]}(User's Preferred Language),date_of_birth: {user_data["dob"]},english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level),created_date: {user_data["created_date"]}(user creation date),standard: {user_data["standard"]}(User's Standard in which user studying)
   '''
    return user_details, actions


